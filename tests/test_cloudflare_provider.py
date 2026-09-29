import json
from datetime import UTC, datetime

import httpx
import pytest

from noema.cloudflare_client import CloudflareCognitionClient
from noema.cloudflare_config import CloudflareConfig
from noema.cognition_config import cognition_config_from_env, cognition_provider_name
from noema.cognition_dashboard import _runtime_providers
from noema.cognition_policy import CognitionPolicy
from noema.cognition_request import build_cognition_request
from noema.openai_config import OpenAIConfig
from noema.opportunity_radar import RadarRow


def market():
    return RadarRow('kalshi:demo','M','Fixture',.65,.5,.51,.14,.01,.02,.11,.02,1000,
                    datetime.now(UTC).isoformat(),1,.1,.9,'pass','research',('e1',))


def config():
    return CloudflareConfig(api_token='cf-test-secret', account_id='test-account',
                            enabled=True)


def test_config_secret_fields_are_hidden_and_auto_prefers_cloudflare(monkeypatch):
    monkeypatch.setenv('NOEMA_COGNITION_PROVIDER','auto')
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN','cf-test-secret')
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID','test-account')
    monkeypatch.setenv('NOEMA_OPENAI_ENABLED','1')
    monkeypatch.setenv('OPENAI_API_KEY','openai-test-secret')
    monkeypatch.setenv('NOEMA_OPENAI_MODEL','gpt-4.1-mini')
    chosen=cognition_config_from_env()
    assert isinstance(chosen,CloudflareConfig)
    assert cognition_provider_name(chosen)=='cloudflare_workers_ai'
    assert 'cf-test-secret' not in repr(chosen)
    assert 'test-account' not in repr(chosen)


def test_cloudflare_pricing_requires_selected_model_match(monkeypatch):
    monkeypatch.setenv('NOEMA_CLOUDFLARE_MODEL','@cf/meta/llama-3.3-70b-instruct-fp8-fast')
    monkeypatch.setenv('NOEMA_CLOUDFLARE_PRICING_MODEL','@cf/meta/llama-3.3-70b-instruct-fp8-fast')
    monkeypatch.setenv('NOEMA_CLOUDFLARE_INPUT_USD_PER_MILLION','0.293')
    monkeypatch.setenv('NOEMA_CLOUDFLARE_OUTPUT_USD_PER_MILLION','2.253')
    policy=CognitionPolicy.from_env(provider='cloudflare')
    assert policy.input_usd_per_million==0.293
    assert policy.output_usd_per_million==2.253
    monkeypatch.setenv('NOEMA_CLOUDFLARE_PRICING_MODEL','different-model')
    assert CognitionPolicy.from_env(provider='cloudflare').input_usd_per_million is None


@pytest.mark.asyncio
async def test_workers_ai_request_uses_scoped_json_schema_and_parses_usage():
    packet={'thesis':'Research only','confidence':.4,'attention_reason':'Fixture evidence',
            'counterarguments':[],'unknowns':['fees'],'requested_research':[],
            'recommended_mode':'collect_more','evidence_ids':['e1']}
    captured=[]
    def handler(request):
        captured.append(request)
        assert request.headers['authorization']=='Bearer cf-test-secret'
        assert 'test-account' in str(request.url)
        body=json.loads(request.content)
        assert 'cf-test-secret' not in request.content.decode()
        assert body['response_format']['type']=='json_schema'
        assert body['response_format']['json_schema']['required']
        return httpx.Response(200,json={'success':True,'result':{
            'response':packet,'usage':{'prompt_tokens':40,'completion_tokens':15,'total_tokens':55}
        }})
    cfg=config()
    client=CloudflareCognitionClient(cfg,httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    body=build_cognition_request(market(),evidence_context=[{'evidence_id':'e1','facts':'fixture'}],
                                 model=cfg.model,reasoning_effort='none',max_output_tokens=100)
    result=await client.reason_about_market(
        market(),evidence_context=[{'evidence_id':'e1','facts':'fixture'}]
    )
    await client.close()
    assert len(captured)==1
    assert result.status=='completed'
    assert result.packet and result.packet.thesis=='Research only'
    assert result.input_tokens==40 and result.output_tokens==15
    assert 'cf-test-secret' not in json.dumps(body)


def test_dashboard_reports_cloudflare_model_health_without_credentials(monkeypatch):
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN','cf-test-secret')
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID','test-account')
    monkeypatch.setenv('NOEMA_CLOUDFLARE_MODEL','@cf/meta/llama-3.3-70b-instruct-fp8-fast')
    monkeypatch.setenv('NOEMA_LOCAL_ENDPOINT','http://127.0.0.1:12434/engines/v1')
    class Response:
        status_code=200
        def __init__(self,payload): self.payload=payload
        def raise_for_status(self): pass
        def json(self): return self.payload
    def get(url,**kwargs):
        if url.endswith('/models'):
            return Response({'data':[]})
        if url.endswith('/ai/models/search'):
            assert kwargs['headers']['Authorization']=='Bearer cf-test-secret'
            return Response({'success':True,'result':[{'name':'@cf/meta/llama-3.3-70b-instruct-fp8-fast'}]})
        return Response({'ok':True,'model':'fixture'})
    monkeypatch.setattr('noema.cognition_dashboard.httpx.get',get)
    report=_runtime_providers(OpenAIConfig(api_key='hidden',model='test-model',enabled=True))
    cloudflare=report['hosted_providers']['cloudflare_workers_ai']
    assert cloudflare['status']=='healthy'
    assert cloudflare['credential_present'] is True
    encoded=json.dumps(report)
    assert all(secret not in encoded for secret in ('cf-test-secret','test-account','hidden'))
