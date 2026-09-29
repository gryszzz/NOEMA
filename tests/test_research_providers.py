import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from noema.agent_identity import AgentIdentity
from noema.cognition_config import cognition_config_from_env
from noema.cognition_dashboard import _runtime_providers
from noema.cognition_request import build_cognition_request
from noema.local_cognition import (
    LocalCognitionClient,
    LocalCognitionConfig,
    max_local_model_size_gib,
)
from noema.openai_client import OpenAICognitionClient
from noema.openai_config import OpenAIConfig
from noema.opportunity_radar import RadarRow
from noema.research_session import SessionStore, choose_research


def market():
    return RadarRow('kalshi:demo','M','Fixture',.65,.5,.51,.14,.01,.02,.11,.02,1000,
                    datetime.now(UTC).isoformat(),1,.1,.9,'pass','research',('e1',))


@pytest.mark.asyncio
async def test_openai_key_only_auth_no_required_project_and_structured_identity():
    packet = {'thesis':'Test hypothesis','confidence':.5,'attention_reason':'Test only',
              'counterarguments':[],'unknowns':['Costs'],'requested_research':[],
              'recommended_mode':'investigate','evidence_ids':['e1']}
    requests=[]
    def transport(request):
        requests.append(request)
        body=json.loads(request.content)
        assert request.headers['authorization']=='Bearer test-only-key'
        assert 'openai-project' not in request.headers
        assert 'test-only-key' not in request.content.decode()
        assert body['store'] is False
        assert 'You are NOEMA' in body['instructions']
        assert 'REGISTERED SPECIALIST ROLE: kalshi-history' in body['instructions']
        assert 'track calibration after resolution' in body['instructions']
        assert body['text']['format']['strict'] is True
        return httpx.Response(200,json={'id':'resp_test','status':'completed','output':[
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(packet)}]}
        ],'usage':{'input_tokens':100,'output_tokens':50,'total_tokens':150}})
    config=OpenAIConfig(api_key='test-only-key',model='test-model',enabled=True)
    assert config.ready and 'test-only-key' not in repr(config)
    client=OpenAICognitionClient(config,httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    result=await client.reason_about_market(market(),evidence_context=[{'evidence_id':'e1','facts':'fixture'}])
    await client.close()
    assert result.status=='completed' and result.total_tokens==150 and len(requests)==1


@pytest.mark.asyncio
async def test_refusal_or_unsupported_financial_action_is_not_an_agent_decision():
    client=OpenAICognitionClient(OpenAIConfig(api_key='test',model='test',enabled=True),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={
            'status':'incomplete','output':[],
        }))))
    with pytest.raises(ValueError,match='complete'):
        await client.reason_about_market(market(),evidence_context=[{'evidence_id':'e1'}])
    await client.close()


def test_optional_project_and_unknown_provider(monkeypatch):
    monkeypatch.setenv('NOEMA_COGNITION_PROVIDER','openai')
    monkeypatch.setenv('OPENAI_API_KEY','test')
    monkeypatch.setenv('NOEMA_OPENAI_MODEL','gpt-4.1-mini')
    monkeypatch.setenv('NOEMA_OPENAI_ENABLED','1')
    monkeypatch.delenv('NOEMA_OPENAI_PROJECT_ID',raising=False)
    monkeypatch.delenv('OPENAI_PROJECT_ID',raising=False)
    assert cognition_config_from_env().ready
    assert not cognition_config_from_env().supports_reasoning_effort
    monkeypatch.setenv('NOEMA_COGNITION_PROVIDER','arbitrary-provider')
    with pytest.raises(ValueError):cognition_config_from_env()


def test_nonreasoning_openai_model_omits_reasoning_override():
    body = build_cognition_request(
        market(), evidence_context=[{'evidence_id':'e1','facts':'fixture'}],
        model='gpt-4.1-mini', reasoning_effort='medium', max_output_tokens=100,
    )
    assert 'reasoning' not in body


def test_role_overlays_cover_only_registered_specialists():
    identity = AgentIdentity()
    assert 'verified, timestamped observations and matured forward labels' in \
        identity.specialist_instructions('trench-1')
    assert identity.specialist_instructions('evidence-critic') == identity.instructions
    assert identity.specialist_role('evidence-critic') is None


@pytest.mark.asyncio
async def test_local_abstraction_accepts_any_installed_model_and_no_credentials():
    model='docker.io/local/hermes:test-fixture'
    def transport(request):
        assert 'authorization' not in request.headers
        if request.method=='GET':return httpx.Response(200,json={'data':[{
            'id':model,'dmr':{'size':'900 MiB'},
        }]})
        body=json.loads(request.content)
        assert body['model']==model
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({
            'trial_id':'idle','rationale':'No evidence','unknowns':[]
        })}}],'usage':{'prompt_tokens':10,'completion_tokens':5}})
    client=LocalCognitionClient(LocalCognitionConfig(model=model),
                               httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    result=await client.structured_research('Test',{}, {})
    await client.close()
    assert result['selection']['trial_id']=='idle'


@pytest.mark.asyncio
async def test_uninstalled_hermes_cannot_be_reported_as_used():
    client=LocalCognitionClient(LocalCognitionConfig(model='hermes'),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'data':[]}))))
    with pytest.raises(ValueError,match='not installed'):
        await client.structured_research('test',{}, {})
    await client.close()


@pytest.mark.asyncio
async def test_local_model_must_have_known_size_under_conservative_ceiling(monkeypatch):
    model = 'docker.io/ai/oversized-fixture:latest'
    calls = []
    def transport(request):
        calls.append(request.method)
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{
                'id': model, 'dmr': {'size': '2.25 GiB'},
            }]})
        pytest.fail('oversized model must be rejected before inference')

    monkeypatch.delenv('NOEMA_LOCAL_MODEL_MAX_SIZE_GIB', raising=False)
    client = LocalCognitionClient(
        LocalCognitionConfig(model=model),
        httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(ValueError, match='1.5 GiB model size ceiling'):
        await client.structured_research('test', {}, {})
    await client.close()
    assert calls == ['GET']


@pytest.mark.asyncio
async def test_local_model_with_unknown_size_fails_closed_before_inference():
    model = 'docker.io/ai/unknown-size:latest'
    calls = []
    def transport(request):
        calls.append(request.method)
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': model}]})
        pytest.fail('model with unknown installed size must not load')

    client = LocalCognitionClient(
        LocalCognitionConfig(model=model),
        httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(ValueError, match='size is unavailable'):
        await client.structured_research('test', {}, {})
    await client.close()
    assert calls == ['GET']


def test_local_model_size_ceiling_is_conservative_and_bounded(monkeypatch):
    monkeypatch.delenv('NOEMA_LOCAL_MODEL_MAX_SIZE_GIB', raising=False)
    assert max_local_model_size_gib() == 1.5


@pytest.mark.asyncio
async def test_oversized_local_model_releases_lease_and_uses_bounded_deterministic_fallback(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv('NOEMA_LOCAL_COGNITION_ENABLED', '1')
    monkeypatch.setenv('NOEMA_CLOUDFLARE_ENABLED', '0')
    monkeypatch.setenv('NOEMA_OPENAI_ENABLED', '0')
    for key in ('CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'OPENAI_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    lease_state = {'released': False}

    class Lease:
        def release(self):
            lease_state['released'] = True

    class OversizedLocalClient:
        config = SimpleNamespace(model='fixture-oversized-model')
        async def resource_eligibility(self):
            return False, 'RESOURCE LIMITED: configured local model exceeds size ceiling'
        async def structured_research(self, *_args):
            pytest.fail('resource-ineligible local model must not be invoked')
        async def close(self):
            pass

    monkeypatch.setattr('noema.research_session.try_acquire', lambda _kind: (Lease(), None))
    monkeypatch.setattr('noema.research_session.LocalCognitionClient', OversizedLocalClient)
    store = SessionStore(str(tmp_path / 'research.db'))
    sid = store.begin_worker('bounded test')
    candidate = (SimpleNamespace(trial_id='trial-a', hypothesis='fixture'), None, 'test')
    chosen = await choose_research(store, sid, [candidate], None)
    assert chosen == 'trial-a'
    assert lease_state['released'] is True
    row = store.conn.execute(
        'SELECT provider,model,objective FROM cognitive_sessions WHERE session_id=?', (sid,),
    ).fetchone()
    assert row == ('deterministic', 'evidence-priority-v1', 'trial-a')
    event = store.conn.execute(
        "SELECT status,detail FROM runtime_events WHERE session_id=? AND stage='resource_admission'",
        (sid,),
    ).fetchone()
    assert event[0] == 'limited' and 'size ceiling' in event[1]
    monkeypatch.setenv('NOEMA_LOCAL_MODEL_MAX_SIZE_GIB', '2.0')
    assert max_local_model_size_gib() == 2.0
    monkeypatch.setenv('NOEMA_LOCAL_MODEL_MAX_SIZE_GIB', '99')
    assert max_local_model_size_gib() == 1.5
    monkeypatch.setenv('NOEMA_LOCAL_MODEL_MAX_SIZE_GIB', 'not-a-size')
    assert max_local_model_size_gib() == 1.5


@pytest.mark.asyncio
async def test_research_selector_receives_registered_specialist_role_context(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv('NOEMA_LOCAL_COGNITION_ENABLED', '1')
    monkeypatch.setenv('NOEMA_CLOUDFLARE_ENABLED', '0')
    monkeypatch.setenv('NOEMA_OPENAI_ENABLED', '0')
    captured = {}

    class Lease:
        def release(self):
            pass

    class LocalClient:
        config = SimpleNamespace(model='fixture-model')

        async def resource_eligibility(self):
            return True, None

        async def structured_research(self, instructions, inputs, _schema):
            captured['instructions'] = instructions
            captured['inputs'] = inputs
            return {
                'selection': {'trial_id': 'web3-trial', 'rationale': 'Forward labels',
                              'unknowns': []},
                'usage': {'prompt_tokens': 10, 'completion_tokens': 4},
            }

        async def close(self):
            pass

    monkeypatch.setattr('noema.research_session.try_acquire', lambda _kind: (Lease(), None))
    monkeypatch.setattr('noema.research_session.LocalCognitionClient', LocalClient)
    store = SessionStore(str(tmp_path / 'role-context.db'))
    session_id = store.begin_worker('bounded role-routing test')
    candidate = (SimpleNamespace(trial_id='web3-trial', hypothesis='Test survival labels'),
                 'trench-1', 'trench_survival_logistic')

    selected = await choose_research(
        store, session_id, [candidate], None, selected_goal="develop_specialist",
    )

    assert selected == 'web3-trial'
    routed = captured['inputs']['candidates'][0]
    assert routed['assigned_specialist'] == 'trench-1'
    assert 'verified, timestamped observations' in routed['specialist_role']
    assert captured['inputs']['selected_operational_goal'] == 'develop the currently evidence-favored specialist'
    assert 'directly advances it' in captured['instructions']
    assert 'Candidate specialist roles are fixed routing context' in captured['instructions']


def test_local_provider_cannot_silently_send_evidence_to_external_host():
    with pytest.raises(ValueError,match='loopback'):
        LocalCognitionConfig(endpoint='https://example.com/v1').validate()


def test_runtime_inventory_reports_verified_models_and_never_secret_values(monkeypatch):
    monkeypatch.setenv('NOEMA_LOCAL_ENDPOINT','http://127.0.0.1:12434/engines/v1')
    monkeypatch.setenv('NOEMA_LOCAL_MODEL','docker.io/ai/gemma3:latest')
    monkeypatch.setenv('OPENAI_API_KEY','do-not-return-this')
    monkeypatch.setenv('GROQ_API_KEY','also-secret')
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN','cf-secret')
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID','cf-account')

    class Response:
        status_code = 200
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    def get(url, **_):
        if url.endswith('/models'):
            return Response({'data': [
                {'id':'docker.io/ai/gemma3:latest','dmr':{
                    'architecture':'gemma3','size':'900 MiB',
                }},
                {'id':'huggingface.co/qwen/qwen3-embedding-0.6b-gguf:Q8_0',
                 'dmr':{'architecture':'qwen3'}},
                {'id':'huggingface.co/tensorblock/qwen3-reranker',
                 'dmr':{'architecture':'qwen3'}},
                {'id':'docker.io/ai/moondream2:latest','dmr':{'architecture':'phi2'}},
            ]})
        return Response({'ok':True,'model':'verified-fixture'})

    monkeypatch.setattr('noema.cognition_dashboard.httpx.get',get)
    config = OpenAIConfig(api_key='do-not-return-this', model='test-model', enabled=True)
    report = _runtime_providers(config)
    local = report['local_model_runner']
    assert local['status'] == 'healthy' and local['selected_model_available']
    assert local['selected_model_size_bytes'] == 900 * 1024**2
    assert local['selected_model_resource_eligible'] is True
    assert local['selected_model_resource_reason'] is None
    capabilities = {row['id']: row['capabilities'] for row in local['models']}
    assert capabilities['docker.io/ai/gemma3:latest'] == ['chat']
    assert capabilities['huggingface.co/qwen/qwen3-embedding-0.6b-gguf:Q8_0'] == ['embedding']
    assert capabilities['huggingface.co/tensorblock/qwen3-reranker'] == ['reranker_model']
    assert capabilities['docker.io/ai/moondream2:latest'] == ['chat', 'vision_model']
    assert report['specialists']['chronos']['status'] == 'healthy'
    assert report['specialists']['finbert']['model'] == 'verified-fixture'
    assert report['hosted_providers']['openai']['credential_present'] is True
    encoded = json.dumps(report)
    assert all(secret not in encoded for secret in ('do-not-return-this','also-secret','cf-secret','cf-account'))


def test_runtime_status_marks_oversized_selected_model_resource_limited(monkeypatch):
    monkeypatch.setenv('NOEMA_LOCAL_ENDPOINT', 'http://127.0.0.1:12434/engines/v1')
    monkeypatch.setenv('NOEMA_LOCAL_MODEL', 'fixture/large-model')
    monkeypatch.delenv('NOEMA_LOCAL_MODEL_MAX_SIZE_GIB', raising=False)
    monkeypatch.setenv('NOEMA_CLOUDFLARE_ENABLED', '0')
    for key in ('CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID'):
        monkeypatch.delenv(key, raising=False)

    class Response:
        status_code = 200
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    def get(url, **_):
        if url.endswith('/models'):
            return Response({'data': [{'id': 'fixture/large-model',
                                      'dmr': {'size': '2.25 GiB'}}]})
        return Response({'ok': True})

    monkeypatch.setattr('noema.cognition_dashboard.httpx.get', get)
    report = _runtime_providers(OpenAIConfig())['local_model_runner']
    assert report['selected_model_available'] is True
    assert report['selected_model_size_bytes'] == int(2.25 * 1024**3)
    assert report['selected_model_resource_eligible'] is False
    assert report['selected_model_resource_reason'].startswith('RESOURCE LIMITED:')
