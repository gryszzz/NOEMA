from noema.trench_desk_runtime import chief_gate
from noema.trench_judgement import Judgement


def test_chief_gate_thresholds():
    picks = [Judgement("a", True, 0.7, "model", 0.6), Judgement("b", True, 0.55, "model", 0.9),
             Judgement("c", True, 0.8, "model", 0.4), Judgement("d", True, 0.8, "rules")]
    ok, log = chief_gate(picks)
    assert [j.mint for j in ok] == ["a"]
    assert [entry["approved"] for entry in log] == [True, False, False, False]
