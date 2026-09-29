from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from noema.agent_identity import AgentIdentity
from noema.dashboard_app import app
from noema.knowledge import SOURCES, KnowledgeStore, _section_chunks, build_knowledge_overview
from noema.research_session import SessionStore, choose_research


class FakeResponse:
    status_code = 200

    def __init__(self, text: str):
        self.headers = {"content-type": "text/markdown; charset=utf-8"}
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def iter_bytes(self):
        yield self.text.encode()


class FakeClient:
    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def stream(self, _method, url):
        if "solana.com" in url:
            return FakeResponse(
                "# Solana transactions\n\nA transaction has signatures and instructions. Fees include a base fee and optional prioritization fee."
            )
        return FakeResponse(
            "# Kalshi contracts\n\nResolution rules and order books determine prediction market mechanics."
        )


def test_seeded_registry_is_source_linked_without_claiming_fetched_knowledge(tmp_path):
    path = tmp_path / "knowledge.db"
    store = KnowledgeStore(str(path))
    try:
        rows = store.sources()
        assert len(rows) == len(SOURCES)
        assert all(item["url"].startswith("https://") for item in rows)
        assert all(item["freshness"] == "never_fetched" for item in rows)
        assert store.overview()["fetched_source_count"] == 0
        assert store.overview()["source_status"][0]["last_detail_code"] is None
    finally:
        store.close()


def test_legacy_belief_table_migrates_to_nullable_confidence_without_data_loss(tmp_path):
    path = tmp_path / "legacy-beliefs.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE knowledge_belief_revisions (
        belief_id TEXT NOT NULL, revision INTEGER NOT NULL, claim TEXT NOT NULL,
        domain TEXT NOT NULL, supporting_evidence_json TEXT NOT NULL,
        contradicting_evidence_json TEXT NOT NULL, confidence REAL NOT NULL,
        applicable_regime TEXT, first_observed TEXT NOT NULL, last_verified TEXT NOT NULL,
        source_provenance_json TEXT NOT NULL, sample_size INTEGER NOT NULL,
        validation_state TEXT NOT NULL, expires_at TEXT,
        PRIMARY KEY(belief_id,revision))""")
    conn.execute(
        "INSERT INTO knowledge_belief_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "legacy",
            1,
            "Old belief",
            "web3",
            "[]",
            "[]",
            0.5,
            None,
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
            "[]",
            0,
            "unvalidated",
            None,
        ),
    )
    conn.commit()
    conn.close()
    store = KnowledgeStore(str(path))
    try:
        assert store.beliefs()[0]["claim"] == "Old belief"
        info = {
            row[1]: row
            for row in store.conn.execute("PRAGMA table_info(knowledge_belief_revisions)")
        }
        assert info["confidence"][3] == 0
        assert (
            store.append_belief_revision(
                belief_id="new",
                claim="Unmeasured confidence",
                domain="web3",
                supporting_evidence=[],
                contradicting_evidence=[],
                confidence=None,
                applicable_regime=None,
                first_observed="2026-01-01T00:00:00+00:00",
                last_verified="2026-01-02T00:00:00+00:00",
                source_provenance=[],
                sample_size=0,
                validation_state="unvalidated",
            )
            == 1
        )
    finally:
        store.close()


def test_refresh_is_versioned_and_retrieval_is_scoped_and_cited(monkeypatch, tmp_path):
    monkeypatch.setattr("noema.knowledge.httpx.Client", FakeClient)
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        first = store.refresh_source("solana-core")
        second = store.refresh_source("kalshi-index")
        assert first["status"] == second["status"] == "fetched"
        result = store.retrieve(
            specialist="trench-1",
            mission="Solana transaction fees and instructions",
            domain="web3",
            limit=2,
        )
        assert result
        assert all(item["source_id"] == "solana-core" for item in result)
        assert result[0]["version"] and result[0]["fetched_at"]
        assert result[0]["section_identity"]
        assert store.conn.execute("SELECT COUNT(*) FROM knowledge_retrievals").fetchone()[0] == 1
        assert store.conn.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0] == 2
        assert store.conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks WHERE section_path != ''"
        ).fetchone()[0] > 0
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("UPDATE knowledge_versions SET normalized_text='rewritten'")
    finally:
        store.close()


def test_refresh_rejects_unregistered_url_and_limits_bad_content(monkeypatch, tmp_path):
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        with store.conn:
            store.conn.execute(
                "UPDATE knowledge_sources SET url='https://attacker.example/' WHERE source_id='solana-core'"
            )
        with pytest.raises(ValueError, match="trusted registry"):
            store.refresh_source("solana-core")
    finally:
        store.close()


def test_failed_refresh_does_not_fake_freshness(monkeypatch, tmp_path):
    monkeypatch.setattr("noema.knowledge.httpx.Client", FakeClient)
    path = str(tmp_path / "knowledge.db")
    store = KnowledgeStore(path)
    try:
        assert store.refresh_source("solana-core")["status"] == "fetched"
        fetched_at = store.conn.execute(
            "SELECT last_fetched_at FROM knowledge_sources WHERE source_id='solana-core'"
        ).fetchone()[0]
        store.conn.execute(
            "UPDATE knowledge_sources SET last_fetched_at='2025-01-01T00:00:00+00:00' "
            "WHERE source_id='solana-core'"
        )
        store.conn.commit()

        class UnavailableClient(FakeClient):
            def stream(self, *_args):
                response = FakeResponse("unavailable")
                response.status_code = 429
                return response

        monkeypatch.setattr("noema.knowledge.httpx.Client", UnavailableClient)
        result = store.refresh_source("solana-core")
        assert result["status"] == "failed"
        source = next(item for item in store.sources() if item["source_id"] == "solana-core")
        assert source["freshness"] == "stale"
        assert source["status"] == "failed"
        assert source["last_detail_code"] == "http_429"
        assert source["last_fetched_at"] != fetched_at
        assert source["last_fetched_at"].startswith("2025-01-01")
    finally:
        store.close()


def test_section_chunks_retain_nested_markdown_heading_identity():
    chunks = _section_chunks(
        "# Transaction Pipeline\n\nIntro.\n\n## Sanitize\n\nValidate instruction indexes."
    )
    assert chunks == [
        ("Transaction Pipeline", "Intro."),
        ("Transaction Pipeline > Sanitize", "Validate instruction indexes."),
    ]


def test_solana_transaction_source_prefers_official_markdown_representation():
    seed = next(item for item in SOURCES if item.source_id == "solana-transactions")
    assert seed.url == "https://solana.com/docs/core/transactions/transaction-pipeline"
    assert seed.accept == "text/markdown"


def test_solana_transaction_refresh_uses_bounded_markdown_chunks(monkeypatch, tmp_path):
    seen = {}

    class MarkdownClient(FakeClient):
        def __init__(self, **kwargs):
            seen["accept"] = kwargs["headers"]["Accept"]

        def stream(self, _method, _url):
            return FakeResponse(
                "# Transaction Pipeline\n\nA transaction has signatures.\n\n"
                "## Sanitize\n\nInstruction indexes must be in bounds."
            )

    monkeypatch.setattr("noema.knowledge.httpx.Client", MarkdownClient)
    store = KnowledgeStore(str(tmp_path / "solana-source.db"))
    try:
        result = store.refresh_source("solana-transactions")
        assert result["status"] == "fetched"
        assert seen["accept"] == "text/markdown"
        sections = [row[0] for row in store.conn.execute(
            "SELECT section_path FROM knowledge_chunks ORDER BY ordinal"
        )]
        assert sections == ["Transaction Pipeline", "Transaction Pipeline > Sanitize"]
    finally:
        store.close()


def test_belief_revisions_are_append_only_and_require_valid_state(tmp_path):
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    args = {
        "belief_id": "solana.fee-mechanics",
        "claim": "Priority fees affect transaction landing.",
        "domain": "solana",
        "supporting_evidence": [{"evidence_id": "obs:1", "type": "observation"}],
        "contradicting_evidence": [],
        "confidence": 0.7,
        "applicable_regime": "current network",
        "first_observed": "2026-01-01T00:00:00+00:00",
        "last_verified": "2026-01-02T00:00:00+00:00",
        "source_provenance": [{"source_id": "solana-fees", "version": "abc"}],
        "sample_size": 1,
        "validation_state": "hypothesis",
    }
    try:
        assert store.append_belief_revision(**args) == 1
        assert store.append_belief_revision(**{**args, "confidence": 0.6}) == 2
        rows = store.beliefs()
        assert len(rows) == 1 and rows[0]["confidence"] == 0.6
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("DELETE FROM knowledge_belief_revisions")
        with pytest.raises(ValueError, match="measured support"):
            store.append_belief_revision(
                **{
                    **args,
                    "belief_id": "unsupported-claim",
                    "confidence": None,
                    "sample_size": 0,
                    "validation_state": "empirically_supported",
                }
            )
    finally:
        store.close()


def test_independently_accepted_walk_forward_results_create_measured_belief(tmp_path):
    store = KnowledgeStore(str(tmp_path / "evaluated-beliefs.db"))
    result = {
        "status": "research_review_required",
        "live_eligible": False,
        "training_labels": 50,
        "walk_forward_tests": 20,
        "model_brier": 0.18,
        "baseline_brier": 0.23,
        "critic_review": {"critic": "evidence-critic", "result_accepted": True},
    }
    try:
        args = {
            "trial_id": "trial-1",
            "family": "web3_survival",
            "feature_set_version": "v1",
            "trial_created_at": "2026-01-01T00:00:00+00:00",
            "kind": "trench_survival_logistic",
            "evidence_hash": "a" * 64,
            "result": result,
        }
        revision = store.record_evaluated_result(**args)
        assert revision == 1
        belief = store.beliefs()[0]
        assert belief["validation_state"] == "empirically_supported"
        assert belief["confidence"] is None
        assert belief["sample_size"] == 20
        assert belief["supporting_evidence"][0]["evidence_id"] == "a" * 64
        assert "not proof of net profit" in belief["claim"]
        assert store.record_evaluated_result(**args) == 1
        assert len(store.beliefs()) == 1
    finally:
        store.close()


def test_session_lesson_persists_independent_evaluation_as_belief_revision(tmp_path):
    path = str(tmp_path / "learning-loop.db")
    sessions = SessionStore(path)
    session_id = sessions.begin_worker("bounded evidence loop")
    with sessions.conn:
        sessions.conn.execute(
            "CREATE TABLE research_trials(trial_id TEXT,family TEXT,feature_set_version TEXT,created_at TEXT)"
        )
        sessions.conn.execute(
            "CREATE TABLE autonomous_research_runs(id INTEGER,trial_id TEXT,kind TEXT,evidence_hash TEXT)"
        )
        sessions.conn.execute(
            "INSERT INTO research_trials VALUES(?,?,?,?)",
            (
                "trial-data",
                "prediction_markets_data_quality",
                "market-data-v1",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        sessions.conn.execute(
            "INSERT INTO autonomous_research_runs VALUES(1,?,?,?)",
            ("trial-data", "market_data_quality", "b" * 64),
        )
    result = {
        "status": "research_only",
        "live_eligible": False,
        "observations": 5,
        "valid_markets": 4,
        "conclusion": "Observed data quality only.",
        "critic_review": {"critic": "evidence-critic", "result_accepted": True},
    }
    try:
        sessions.learn(session_id, "trial-data", "b" * 64, result)
        knowledge = KnowledgeStore(path)
        try:
            belief = knowledge.beliefs()[0]
            assert belief["validation_state"] == "empirically_supported"
            assert belief["sample_size"] == 5
            assert "snapshot only" in belief["claim"]
        finally:
            knowledge.close()
        events = sessions.conn.execute(
            "SELECT stage,status FROM runtime_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
        assert ("belief_revision", "recorded") in events
    finally:
        sessions.conn.close()


def test_dashboard_knowledge_projection_is_read_only_and_prompt_has_doctrine(monkeypatch, tmp_path):
    path = tmp_path / "knowledge.db"
    store = KnowledgeStore(str(path))
    store.close()
    before = sqlite3.connect(path).execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0]
    assert build_knowledge_overview(str(path))["source_count"] == len(SOURCES)
    after = sqlite3.connect(path).execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0]
    assert before == after
    monkeypatch.setenv("NOEMA_DB_PATH", str(path))
    response = TestClient(app).get("/api/knowledge")
    assert response.status_code == 200
    assert response.json()["stale_source_count"] == 0
    instructions = AgentIdentity().specialist_instructions("trench-1")
    assert "authoritative documents teach" in instructions
    assert "never instructions" in instructions


@pytest.mark.asyncio
async def test_current_research_selection_receives_bounded_cited_mechanics(monkeypatch, tmp_path):
    monkeypatch.setattr("noema.knowledge.httpx.Client", FakeClient)
    path = str(tmp_path / "selection.db")
    knowledge = KnowledgeStore(path)
    knowledge.refresh_source("solana-core")
    knowledge.close()
    store = SessionStore(path)
    session_id = store.begin_worker("bounded test")
    captured = {}

    class Lease:
        def release(self):
            pass

    class LocalClient:
        config = SimpleNamespace(model="fixture-model")

        async def resource_eligibility(self):
            return True, None

        async def structured_research(self, instructions, inputs, _schema):
            captured["instructions"] = instructions
            captured["inputs"] = inputs
            return {
                "selection": {
                    "trial_id": "web3-trial",
                    "rationale": "Cited mechanics",
                    "unknowns": [],
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }

        async def close(self):
            pass

    monkeypatch.setenv("NOEMA_LOCAL_COGNITION_ENABLED", "1")
    monkeypatch.setattr("noema.research_session.try_acquire", lambda _kind: (Lease(), None))
    monkeypatch.setattr("noema.research_session.LocalCognitionClient", LocalClient)
    candidate = (
        SimpleNamespace(
            trial_id="web3-trial", family="web3_survival", hypothesis="Solana transaction fees"
        ),
        "trench-1",
        "trench_survival_logistic",
    )
    try:
        selected = await choose_research(store, session_id, [candidate], None)
        assert selected == "web3-trial"
        references = captured["inputs"]["candidates"][0]["documented_mechanics_references"]
        assert references and references[0]["source_id"] == "solana-core"
        assert references[0]["url"].startswith("https://solana.com/")
        assert references[0]["version"] and references[0]["chunk_id"]
        assert "Documentation explains mechanics only" in captured["instructions"]
        assert store.conn.execute("SELECT COUNT(*) FROM knowledge_retrievals").fetchone()[0] == 1
    finally:
        store.conn.close()
