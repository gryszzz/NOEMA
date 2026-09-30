import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from noema import autonomous_research, resource_control
from noema.autonomous_research import (
    ResearchWorkPolicy,
    ResearchWorkStore,
    _execute_worker,
    register_trench_trial_if_ready,
    run_research_work,
    snapshot_evidence,
)
from noema.ecosystem_controller import review_research_ecosystem
from noema.history_forecaster import MODEL_VERSION
from noema.mission_store import MissionStore
from noema.models import MarketSnapshot
from noema.operations_dashboard import build_operations
from noema.provenance import EvidenceStore
from noema.research_session import SessionStore
from noema.research_trials import ResearchTrialStore
from noema.research_worker import cost_threshold_sweep
from noema.soak import SoakStore


def observed_database(tmp_path):
    path = str(tmp_path / 'test.db')
    store = SoakStore(path)
    store.append_market(MarketSnapshot(
        'kalshi:demo', 'SERIES-EVENT-A', 'Fixture only', .4, .5, .5, .6, 1000,
        None, 'Explicit resolution', captured_at=datetime.now(UTC),
    ))
    store.conn.close()
    return path


def test_openclaw_audit_route_requires_material_evidence_or_unresolved_gap():
    assert not autonomous_research.audit_warrants_openclaw(
        'market_data_quality', {'observations': 20, 'valid_markets': 20,
                                'markets_with_rules': 20, 'markets_with_two_sided_quotes': 20},
    )
    assert autonomous_research.audit_warrants_openclaw(
        'market_data_quality', {'observations': 20, 'valid_markets': 18,
                                'markets_with_rules': 20, 'markets_with_two_sided_quotes': 20},
    )
    assert autonomous_research.audit_warrants_openclaw(
        'cost_threshold_sweep', {'variants': [{'events': 11}]},
    )
    assert autonomous_research.audit_warrants_openclaw(
        'trench_survival_logistic', {'walk_forward_tests': 10, 'model_brier': 0.18},
    )
    assert autonomous_research.audit_warrants_openclaw(
        'commercial_opportunity_scan', {'visible_payment_signal_count': 1},
    )
    assert not autonomous_research.audit_warrants_openclaw(
        'commercial_opportunity_scan', {'visible_payment_signal_count': 0},
    )


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch, tmp_path):
    for key in ('NOEMA_MCP_ENABLED', 'NOEMA_LOCAL_COGNITION_ENABLED', 'NOEMA_OPENAI_ENABLED'):
        monkeypatch.setenv(key, '0')
    monkeypatch.setenv('NOEMA_RESOURCE_LOCK_DIR', str(tmp_path / 'resource-locks'))
    monkeypatch.setattr(resource_control, 'memory_snapshot', lambda: {
        'available_percent': 80, 'source': 'test',
    })


@pytest.mark.asyncio
async def test_real_subprocess_openclaw_handoff_is_critic_checked_and_learned(tmp_path, monkeypatch):
    path = observed_database(tmp_path)
    monkeypatch.setenv('NOEMA_OPENCLAW_ENABLED', '1')
    async def review(**_kwargs):
        return {
            'status': 'completed', 'cleanup_status': 'removed', 'input_tokens': 17,
            'output_tokens': 4, 'cost_usd': 0.001, 'model': 'fixture-openclaw',
            'result': {
                'status': 'reviewed', 'verified_metrics': {'observations': 1},
                'limitation': 'The sample contains one frozen observation.',
                'falsification_test': 'Repeat with a larger forward sample.',
                'next_priority': 'require_forward_validation', 'live_eligible': False,
            },
        }
    monkeypatch.setattr(autonomous_research, 'run_review', review)
    plan = review_research_ecosystem(path)
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE market_snapshots SET valid=0')
    result = await run_research_work(path, plan, ResearchWorkPolicy(enabled=True))
    assert result['status'] == 'completed'
    with sqlite3.connect(path) as conn:
        row = conn.execute('SELECT status,compute_cost_usd,result_json FROM autonomous_research_runs').fetchone()
        assert row[0] == 'completed' and row[1] is None
        payload = json.loads(row[2])
        assert payload['live_eligible'] is False
        assert payload['observations'] == 1
        assert conn.execute('SELECT COUNT(*) FROM research_lessons').fetchone()[0] == 2
        assert conn.execute('SELECT name FROM cognitive_identities').fetchone()[0] == 'NOEMA'
        mission = conn.execute(
            'SELECT mission_id,status,specialist,capability_grants_json,resource_grant_json,lesson_id '
            'FROM missions'
        ).fetchone()
        assert mission[1] == 'completed' and mission[2] == 'kalshi-history'
        assert set(json.loads(mission[3])) == {'execute_allowlisted_research_handler', 'read_frozen_evidence:market_data_quality'}
        grants = json.loads(mission[4])
        assert grants['network'] == 'denied' and grants['live_execution'] is False
        assert mission[5] is not None
        handoff = conn.execute(
            'SELECT from_specialist,to_specialist,status,result_json FROM mission_handoffs'
        ).fetchone()
        assert handoff[0:3] == ('kalshi-history', 'evidence-critic', 'completed')
        assert json.loads(handoff[3])['verdict'] == 'PASS'
        openclaw_handoff = conn.execute(
            "SELECT status,result_json FROM mission_handoffs WHERE to_specialist='openclaw-reviewer'"
        ).fetchone()
        assert openclaw_handoff[0] == 'completed'
        openclaw_result = json.loads(openclaw_handoff[1])
        assert openclaw_result['result']['critic_review']['verdict'] == 'PASS'
        assert openclaw_result['elapsed_seconds'] is not None
        assert openclaw_result['tools_used'] == ['openclaw-agent', 'openclaw-sandbox']
        assert conn.execute(
            "SELECT status,input_tokens,output_tokens,estimated_model_cost_usd FROM cognitive_sessions "
            "WHERE provider='openclaw'"
        ).fetchone() == ('completed', 17, 4, 0.001)
        worker_lesson = conn.execute(
            "SELECT mission_id,next_priority FROM research_lessons WHERE session_id=("
            "SELECT session_id FROM cognitive_sessions WHERE provider='openclaw')"
        ).fetchone()
        assert worker_lesson == (mission[0], 'require_forward_validation')
        events = [row[0] for row in conn.execute(
            'SELECT event_type FROM mission_events WHERE mission_id=? ORDER BY id', (mission[0],)
        )]
        assert events == ['opportunity_discovered', 'claimed', 'work_started',
                          'handoff_requested', 'handoff_completed', 'critic_evaluation',
                          'handoff_requested', 'handoff_accepted', 'handoff_running',
                          'openclaw_lesson_persisted', 'handoff_completed']
        assert conn.execute('SELECT mission_id FROM research_lessons').fetchone()[0] == mission[0]
    updated_plan = review_research_ecosystem(path)
    before_share = next(item.attention_fraction for item in plan.allocations
                        if item.specialist == 'kalshi-history')
    after_share = next(item.attention_fraction for item in updated_plan.allocations
                       if item.specialist == 'kalshi-history')
    assert after_share < before_share
    repeated = await run_research_work(path, updated_plan, ResearchWorkPolicy(enabled=True))
    assert repeated['status'] == 'idle'
    assert 'prior lesson' in repeated['reason']
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM missions').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM autonomous_research_runs').fetchone()[0] == 1
    operations = build_operations(path)
    assert operations['primary_cognition']['provider'] == 'deterministic'
    assert operations['sections']['sessions']['rows'][0]['status'] == 'completed'
    assert len(operations['sections']['research_runs']['rows']) == 1
    assert operations['sections']['missions']['rows'][0]['status'] == 'completed'
    assert operations['sections']['mission_events']['status'] == 'recorded'
    assert operations['sections']['handoffs']['rows'][0]['to_specialist'] == 'openclaw-reviewer'
    review = operations['attention_allocations']['mission_review']
    assert review['outcome'] == 'DECREASE'
    assert review['specialist'] == 'kalshi-history'
    assert review['current_attention_fraction'] == pytest.approx(after_share)
    assert operations['sections']['missions']['rows'][0]['measurement'][
        'allocation_follow_up']['outcome'] == 'DECREASE'


@pytest.mark.asyncio
async def test_no_authority_and_missing_plan_do_not_create_work(tmp_path):
    path = observed_database(tmp_path)
    assert (await run_research_work(path, None, ResearchWorkPolicy()))['status'] == 'disabled'
    assert (await run_research_work(path, None, ResearchWorkPolicy(enabled=True)))['status'] == 'idle'
    with sqlite3.connect(path) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='autonomous_research_runs'").fetchone()


@pytest.mark.asyncio
async def test_restore_perception_goal_does_not_dispatch_an_unrelated_experiment(tmp_path):
    path = str(tmp_path / 'goal.db')
    result = await run_research_work(
        path, None, ResearchWorkPolicy(enabled=True), selected_goal='restore_market_perception',
    )
    assert result['status'] == 'idle'
    assert 'restoring current market perception' in result['reason']
    with sqlite3.connect(path) as conn:
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='autonomous_research_runs'"
        ).fetchone()


@pytest.mark.asyncio
async def test_worker_timeout_terminates_process(tmp_path):
    path = observed_database(tmp_path)
    status, _ = await _execute_worker('market_data_quality', path, .00001)
    assert status == 'timed_out'


def test_snapshot_freezes_only_required_evidence_and_rejects_truncation(tmp_path):
    path = observed_database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE private_context(secret TEXT)')
        conn.execute("INSERT INTO private_context VALUES ('do not copy')")
    frozen = str(tmp_path / 'frozen.db')
    digest = snapshot_evidence(path, frozen, 'market_data_quality', 100)
    assert len(digest) == 64
    with sqlite3.connect(frozen) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='private_context'").fetchone()
        assert conn.execute('SELECT COUNT(*) FROM market_snapshots').fetchone()[0] == 1
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM market_snapshots")
    with sqlite3.connect(frozen) as conn:
        assert conn.execute('SELECT COUNT(*) FROM market_snapshots').fetchone()[0] == 1


def test_claims_are_atomic_and_failed_attempts_consume_daily_budget(tmp_path):
    path = observed_database(tmp_path)
    trials = ResearchTrialStore(path)
    tid = trials.register(family='test', hypothesis='fixture', params={}, feature_set_version='v1')
    trial = trials.get(tid)
    first, second = ResearchWorkStore(path), ResearchWorkStore(path)
    policy = ResearchWorkPolicy(enabled=True, max_runs_per_day=1)
    run_id = first.claim(trial, 'kalshi-history', 'market_data_quality', 'digest', policy)
    assert run_id
    assert second.claim(trial, 'kalshi-history', 'market_data_quality', 'new', policy) is None
    first.finish(run_id, 'failed', .1, {'reason': 'test'})
    assert second.claim(trial, 'kalshi-history', 'market_data_quality', 'new', policy) is None


def test_cross_venue_paper_records_do_not_consume_local_worker_quota(tmp_path):
    path = observed_database(tmp_path)
    store = ResearchWorkStore(path)
    now = datetime.now(UTC).isoformat()
    for index in range(6):
        store.conn.execute(
            "INSERT INTO autonomous_research_runs "
            "(trial_id,specialist,kind,evidence_hash,worker_version,status,created_at,"
            "completed_at,deadline_at) VALUES(?,?,?,?,?,'completed',?,?,?)",
            (f'paper-{index}', 'kalshi-history', 'cross_venue_paper_experiment',
             f'paper-digest-{index}', 'cross-venue-paper-v1', now, now, now),
        )
    store.conn.commit()
    trials = ResearchTrialStore(path)
    trial_id = trials.register(family='test', hypothesis='bounded worker fixture',
                               params={}, feature_set_version='v1')
    policy = ResearchWorkPolicy(enabled=True, max_runs_per_day=1)
    run_id = store.claim(trials.get(trial_id), 'kalshi-history',
                         'market_data_quality', 'worker-digest', policy)
    assert run_id is not None
    store.finish(run_id, 'failed', .1, {'reason': 'fixture consumes reservation'})
    another_id = trials.register(family='test', hypothesis='daily cap fixture',
                                 params={}, feature_set_version='v1')
    assert store.claim(trials.get(another_id), 'kalshi-history',
                       'market_data_quality', 'worker-digest-2', policy) is None
    store.conn.close()


def test_session_cooldown_covers_failed_cognition(tmp_path):
    path = observed_database(tmp_path)
    store = SessionStore(path)
    sid = store.begin()
    store.finish(sid, 'failed', {'reason': 'test'})
    assert store.begin() is None


@pytest.mark.asyncio
async def test_cooldown_persists_one_queued_mission_without_claiming_work(tmp_path):
    path = observed_database(tmp_path)
    sessions = SessionStore(path)
    sid = sessions.begin()
    sessions.finish(sid, 'completed', {'result': 'fixture'})
    sessions.conn.close()

    plan = review_research_ecosystem(path)
    policy = ResearchWorkPolicy(enabled=True)
    first = await run_research_work(path, plan, policy)
    second = await run_research_work(path, plan, policy)
    assert first['status'] == second['status'] == 'queued'
    assert first['mission_id'] == second['mission_id']
    assert first['reason'] == 'research session cooldown active'
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM cognitive_sessions').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM autonomous_research_runs').fetchone()[0] == 0
        row = conn.execute('SELECT status,session_id,run_id FROM missions').fetchone()
        assert row == ('queued', None, None)
        assert conn.execute(
            "SELECT COUNT(*) FROM mission_events WHERE event_type='resource_queue'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_prior_mission_lesson_passes_unsupported_repeat_without_spending_resources(tmp_path):
    path = observed_database(tmp_path)
    plan = review_research_ecosystem(path)
    trials = ResearchTrialStore(path)
    hypothesis = ("Observed market data has sufficient validation, quote and resolution "
                  "coverage to justify investigating a reusable economic data service.")
    trial_id = trials.register(
        family="prediction_markets_data_quality",
        hypothesis=hypothesis,
        params={"experiment": "market_data_quality", "version": "v1"},
        feature_set_version="market-data-v1",
    )
    trials.conn.close()
    sessions = SessionStore(path)
    with sessions.conn:
        sessions.conn.execute(
            "INSERT INTO research_lessons(session_id,trial_id,created_at,next_priority,evidence_hash,lesson,mission_id) "
            "VALUES('prior-session',?,?,?,?,?,?)",
            (trial_id, datetime.now(UTC).isoformat(),
             "validate_demand_and_all_in_costs", "prior-evidence",
             "Feed coverage does not establish demand", "prior-mission"),
        )
    sessions.conn.close()
    missions = MissionStore(path)
    pending_id = missions.discover(
        trial_id=trial_id, evidence_hash="pending-evidence",
        objective=hypothesis, specialist="kalshi-history",
    )
    missions.queue(pending_id, reason="waiting on research session")
    missions.close()

    outcome = await run_research_work(path, plan, ResearchWorkPolicy(enabled=True))
    assert outcome["status"] == "passed"
    assert outcome["mission_id"] == pending_id
    with sqlite3.connect(path) as conn:
        status, result = conn.execute(
            "SELECT status,result_json FROM missions WHERE mission_id=?", (pending_id,),
        ).fetchone()
        assert status == "passed"
        assert json.loads(result)["financial_execution"] is False
        assert conn.execute("SELECT COUNT(*) FROM autonomous_research_runs").fetchone()[0] == 0


def test_queued_opportunity_refresh_does_not_leave_duplicate_pending_missions(tmp_path):
    store = MissionStore(str(tmp_path / 'queued.db'))
    old = store.discover(trial_id='trial', evidence_hash='a' * 64,
                         objective='Research current evidence', specialist='quant')
    store.queue(old, reason='Cooldown')
    newer = store.discover(trial_id='trial', evidence_hash='b' * 64,
                           objective='Research refreshed evidence', specialist='quant')
    store.queue(newer, reason='Cooldown')
    assert old == newer
    assert store.conn.execute('SELECT COUNT(*) FROM missions').fetchone()[0] == 1
    assert store.conn.execute("SELECT status FROM missions").fetchone()[0] == 'queued'
    assert store.conn.execute(
        "SELECT COUNT(*) FROM mission_events WHERE event_type='resource_queue'"
    ).fetchone()[0] == 1
    store.close()


def test_research_session_cooldown_is_bounded_and_ignores_worker_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv('NOEMA_RESEARCH_SESSION_COOLDOWN_SECONDS', '300')
    path = observed_database(tmp_path)
    store = SessionStore(path)
    store.begin_worker('bounded reviewer task')
    assert store.begin() is not None
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE cognitive_sessions SET created_at=? WHERE provider IS NULL",
                     ((datetime.now(UTC)-timedelta(seconds=301)).isoformat(),))
    assert store.begin() is not None
    monkeypatch.setenv('NOEMA_RESEARCH_SESSION_COOLDOWN_SECONDS', '15')
    with pytest.raises(ValueError, match='between 60'):
        store.begin()


def test_future_quote_cannot_supply_margin_to_past_settlement(tmp_path):
    path = str(tmp_path / 'quotes.db')
    now = datetime.now(UTC)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE paper_quotes(id,venue,market_id,selected,quoted_at,model_version,forecast_at,quote_json,lower_bound)')
        conn.execute('CREATE TABLE outcomes(venue,market_id,outcome_yes,resolved_at,first_seen_at)')
        for i, at, lower in ((1, now + timedelta(days=1), .99), (2, now-timedelta(days=2), .51)):
            conn.execute('INSERT INTO paper_quotes VALUES (?,?,?,?,?,?,?,?,?)', (
                i, 'kalshi:demo', 'M', 1, at.isoformat(), MODEL_VERSION,
                (at-timedelta(seconds=1)).isoformat(),
                json.dumps({'total_debit_usd':'.50','contracts':'1'}), lower,
            ))
        conn.execute('INSERT INTO outcomes VALUES (?,?,?,?,?)', (
            'kalshi:demo', 'M', 1, (now-timedelta(days=1)).isoformat(),
            (now-timedelta(days=1)).isoformat(),
        ))
    result = cost_threshold_sweep(path)
    assert all(v['markets'] == 0 for v in result['variants'])


def test_experiment_outcomes_reduce_real_future_allocation(tmp_path):
    path = observed_database(tmp_path)
    original = review_research_ecosystem(path)
    trials = ResearchTrialStore(path)
    tid = trials.register(family='test', hypothesis='fixture', params={}, feature_set_version='v1')
    store = ResearchWorkStore(path)
    run_id = store.claim(trials.get(tid),'kalshi-history','market_data_quality','fixture',ResearchWorkPolicy())
    store.finish(run_id,'completed',1,{'valid_markets':1,'observations':2})
    updated = review_research_ecosystem(path)
    before = next(x.attention_fraction for x in original.allocations if x.specialist=='kalshi-history')
    after = next(x.attention_fraction for x in updated.allocations if x.specialist=='kalshi-history')
    assert after == pytest.approx(before/2)
    assert updated.idle_fraction > original.idle_fraction


def test_trench_trial_waits_for_predeclared_forward_label_minimum(tmp_path, monkeypatch):
    path = str(tmp_path / 'trench-trial.db')
    trials = ResearchTrialStore(path)
    monkeypatch.setattr(autonomous_research, 'load_verified_examples', lambda _path: [None] * 69)
    assert register_trench_trial_if_ready(trials, path) is None
    assert trials.recent(family='trench_survival') == []

    monkeypatch.setattr(autonomous_research, 'load_verified_examples', lambda _path: [None] * 70)
    trial_id = register_trench_trial_if_ready(trials, path)
    trial = trials.get(trial_id)
    assert trial is not None
    assert trial.feature_set_version == 'trench-v1'
    assert autonomous_research.handler_for(trial) == ('trench-1', 'trench_survival_logistic')


@pytest.mark.asyncio
async def test_idle_attention_runs_commercial_qualification_and_records_zero_receipt(
    tmp_path, monkeypatch,
):
    path = observed_database(tmp_path)
    plan = review_research_ecosystem(path)
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM market_snapshots')
    monkeypatch.setenv('NOEMA_MCP_ENABLED', '1')
    search_calls = 0

    async def public_search(store, session_id):
        nonlocal search_calls
        search_calls += 1
        evidence_id = 'mcp:commercial:test-session'
        payload = {
            'query': 'paid bounty reward fixed price data API automation report',
            'source': 'GitHub public issue search', 'total_count': 0, 'issues': [],
            'issue_bodies_included': False, 'demand_verified': False, 'payment_verified': False,
        }
        evidence = EvidenceStore(path)
        try:
            evidence.append(evidence_id=evidence_id, source='github:search_issues',
                            source_type='public_paid_work_discovery', observed_at=datetime.now(UTC),
                            payload=payload)
        finally:
            evidence.conn.close()
        return evidence_id

    monkeypatch.setattr(autonomous_research, 'gather_commercial_opportunity_evidence', public_search)
    result = await run_research_work(path, plan, ResearchWorkPolicy(enabled=True))
    assert result['status'] == 'completed'
    assert result['mission_id']
    with sqlite3.connect(path) as conn:
        mission = conn.execute(
            'SELECT trial_id,status,specialist,result_json,lesson_id FROM missions'
        ).fetchone()
        assert mission[1:3] == ('completed', 'NOEMA')
        payload = json.loads(mission[3])
        assert payload['status'] == 'no_verified_buyer_opportunity'
        assert payload['verified_buyer_count'] == payload['verified_payout_count'] == 0
        assert payload['mission_cash_receipt_usd'] == '0'
        assert payload['revenue_test_status'] == 'not_tested'
        assert payload['realized_net_value_usd'] is None
        assert payload['critic_review']['verdict'] == 'PASS'
        assert mission[4] is not None
        assert conn.execute(
            'SELECT next_priority FROM research_lessons WHERE mission_id=?', (result['mission_id'],)
        ).fetchone()[0] == 'find_verifiable_buyer_and_safe_delivery_channel'
    operations = build_operations(path)
    lanes = {row['key']: row for row in operations['economic_lanes']['lanes']}
    assert lanes['agent_services']['trial_count'] == 1
    assert lanes['agent_services']['run_count'] == 1
    assert lanes['agent_services']['receipts_usd'] is None
    mission_projection = operations['sections']['missions']['rows'][0]['measurement']
    information = mission_projection['information_outcome']
    assert information['verified_buyer_count'] == 0
    assert information['mission_cash_receipt_usd'] == '0'
    assert information['revenue_test_status'] == 'not_tested'
    assert information['attributable_costs_usd']['compute'] is None
    follow_up = mission_projection['allocation_follow_up']
    assert follow_up['status'] == 'observed'
    assert follow_up['outcome'] == 'NO_CHANGE'
    assert follow_up['specialist'] == 'NOEMA'
    assert follow_up['current_attention_fraction'] is None
    assert follow_up['attention_delta'] is None
    assert operations['attention_allocations']['mission_review']['outcome'] == 'NO_CHANGE'
    repeated = await run_research_work(path, plan, ResearchWorkPolicy(enabled=True))
    assert repeated['status'] == 'idle'
    assert 'new verified buyer or delivery source' in repeated['reason']
    assert search_calls == 1
