import pytest

from noema.mission_store import MissionStore


def test_mission_claim_is_idempotent_specialist_bound_and_grants_are_explicit(tmp_path):
    store = MissionStore(str(tmp_path / 'mission.db'))
    mission = store.discover(trial_id='trial', evidence_hash='a' * 64,
                             objective='Inspect a bounded paper hypothesis', specialist='quant')
    assert store.discover(trial_id='trial', evidence_hash='a' * 64,
                          objective='Inspect a bounded paper hypothesis', specialist='quant') == mission
    assert not store.claim(
        mission, specialist='other', session_id='s', run_id=1, evidence_hash='a' * 64,
        capability_grants=['execute_allowlisted_research_handler'],
        resource_grant={'network': 'denied', 'live_execution': False},
    )
    assert store.claim(
        mission, specialist='quant', session_id='s', run_id=1, evidence_hash='a' * 64,
        capability_grants=['execute_allowlisted_research_handler'],
        resource_grant={'network': 'denied', 'secrets': 'none', 'live_execution': False},
    )
    assert not store.claim(
        mission, specialist='quant', session_id='s2', run_id=2, evidence_hash='a' * 64,
        capability_grants=['execute_allowlisted_research_handler'],
        resource_grant={'network': 'denied', 'live_execution': False},
    )
    store.close()


def test_new_evidence_refreshes_one_unclaimed_mission_and_keeps_audit_event(tmp_path):
    store = MissionStore(str(tmp_path / 'refresh.db'))
    mission = store.discover(trial_id='trial', evidence_hash='a' * 64,
                             objective='Inspect current evidence', specialist='quant')
    refreshed = store.discover(trial_id='trial', evidence_hash='b' * 64,
                               objective='Inspect refreshed evidence', specialist='quant')
    assert refreshed == mission
    assert store.conn.execute('SELECT COUNT(*) FROM missions').fetchone()[0] == 1
    row = store.conn.execute('SELECT evidence_hash,objective,status FROM missions').fetchone()
    assert tuple(row) == ('b' * 64, 'Inspect refreshed evidence', 'discovered')
    events = store.conn.execute(
        'SELECT event_type,status FROM mission_events ORDER BY id'
    ).fetchall()
    assert [tuple(event) for event in events] == [
        ('opportunity_discovered', 'discovered'), ('evidence_refreshed', 'discovered'),
    ]
    store.close()


def test_handoffs_reject_secret_or_live_authority_and_record_result(tmp_path):
    store = MissionStore(str(tmp_path / 'handoff.db'))
    mission = store.discover(trial_id='trial', evidence_hash='b' * 64,
                             objective='Review this evidence', specialist='quant')
    with pytest.raises(ValueError, match='allowlist'):
        store.request_handoff(mission, from_specialist='quant', to_specialist='critic',
                              objective='Review', capability_grants=['use_signer'],
                              resource_grant={'live_execution': False})
    with pytest.raises(ValueError, match='live execution'):
        store.request_handoff(mission, from_specialist='quant', to_specialist='critic',
                              objective='Review', capability_grants=['read_research_result'],
                              resource_grant={'live_execution': True})
    handoff = store.request_handoff(
        mission, from_specialist='quant', to_specialist='evidence-critic',
        objective='Check result integrity', capability_grants=['read_research_result'],
        resource_grant={'network': 'denied', 'inference': False, 'live_execution': False},
        contract={'mission_id': mission, 'allowed_tools': ['exec'], 'timeout_seconds': 30},
    )
    requested = store.conn.execute(
        "SELECT payload_json FROM mission_events WHERE event_type='handoff_requested'"
    ).fetchone()
    assert '"allowed_tools": ["exec"]' in requested['payload_json']
    store.finish_handoff(handoff, status='completed', result={'verdict': 'PASS'})
    row = store.conn.execute('SELECT status,result_json FROM mission_handoffs').fetchone()
    assert row['status'] == 'completed' and 'PASS' in row['result_json']
    store.close()
