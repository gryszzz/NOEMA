import asyncio
import json

from noema.trench_judgement import judge_candidates, rules_ranking

CANDS = [
    {"mint": "A", "liquidity_usd": 20_000, "top10_holder_fraction": 0.5, "price_change_m5": 1},
    {"mint": "B", "liquidity_usd": 150_000, "top10_holder_fraction": 0.2, "price_change_m5": 1},
]


def test_rules_ranking_prefers_deep_low_concentration():
    assert [j.mint for j in rules_ranking(CANDS)] == ["B", "A"]


def test_disabled_falls_back_to_rules(tmp_path, monkeypatch):
    monkeypatch.delenv("NOEMA_DESK_JUDGEMENT_ENABLED", raising=False)
    out, status = asyncio.run(judge_candidates(str(tmp_path / "j.db"), CANDS))
    assert status["status"] == "rules_only" and out[0].source == "rules"


class _FakeClient:
    async def structured_research(self, body, trace_metadata=None):
        assert body["text"]["format"]["type"] == "json_schema"
        text = json.dumps({"judgements": [
            {"mint": "A", "trade": True, "probability_up_1h": 0.62},
            {"mint": "B", "trade": False, "probability_up_1h": 0.3},
            {"mint": "ZZZ", "trade": True, "probability_up_1h": 0.99},
        ]})
        return {"status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": text}]}]}


def _enable(monkeypatch, budget="0.50"):
    for key, value in {
        "NOEMA_DESK_JUDGEMENT_ENABLED": "1", "NOEMA_COGNITION_PROVIDER": "openai",
        "NOEMA_OPENAI_ENABLED": "1", "OPENAI_API_KEY": "test-key",
        "NOEMA_OPENAI_MODEL": "gpt-4.1-mini", "NOEMA_OPENAI_PRICING_MODEL": "gpt-4.1-mini",
        "NOEMA_OPENAI_INPUT_USD_PER_MILLION": "0.4", "NOEMA_OPENAI_OUTPUT_USD_PER_MILLION": "1.6",
        "NOEMA_COGNITION_MAX_ESTIMATED_USD_PER_DAY": "2",
        "NOEMA_DESK_JUDGEMENT_MAX_USD_PER_DAY": budget,
    }.items():
        monkeypatch.setenv(key, value)


def test_model_judgement_typed_and_filtered(tmp_path, monkeypatch):
    _enable(monkeypatch)
    out, status = asyncio.run(judge_candidates(str(tmp_path / "j.db"), CANDS,
                                               client=_FakeClient()))
    assert status["status"] == "model"
    assert [(j.mint, j.trade) for j in out] == [("A", True), ("B", False)]


def test_budget_exhausted_uses_rules(tmp_path, monkeypatch):
    _enable(monkeypatch, budget="0.0000001")
    out, status = asyncio.run(judge_candidates(str(tmp_path / "j.db"), CANDS,
                                               client=_FakeClient()))
    assert status["reason"] == "daily judgement budget exhausted"
    assert out[0].source == "rules"
