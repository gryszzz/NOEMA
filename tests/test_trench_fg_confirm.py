import json

from noema import trench72
from noema import trench_fg_confirm as fg


def test_sets_and_bounds_match_study_layout():
    assert fg.epoch_bounds(1062, 1064) == (trench72.START_SLOT, trench72.END_SLOT)
    assert fg.SETS["A"] == (1058, 1061) and fg.SETS["B"] == (1052, 1057)
    a, b, r = (set(fg.set_epochs(k)) for k in ("A", "B", "R"))
    assert not (a & b) and not (a & r) and not (b & r)
    assert fg.VERDICT_SETS == ("A", "B")


def test_frozen_parameters_are_the_studys():
    ex = trench72._exits()
    assert ex[fg.G_EXIT] == {"tmax": 600, "sl": 0.2, "ladder": [(0.4, 1.0)], "exit_prog": 0.95}
    assert ex[fg.F_EXIT] == {"tmax": 120, "sl": 0.079, "ladder": [(0.30, 1.0)]}
    assert (fg.L_PRIMARY, fg.SIZE_SOL, fg.F_BASE, fg.F_SNIPER_SHARE) == (5, 0.1, "B2", 0.3)
    assert [m for m, _ in fg.G_MODES] == [0, 1, 2]


def test_gapped_mask():
    assert fg.gapped_mask([10, 100, 200], [50, 150, 250], []) == [False, False, False]
    assert fg.gapped_mask([10, 100, 200], [50, 150, 250], [150, 5, 199]) == [False, True, False]
    assert fg.gapped_mask([10], [10], [10]) == [True]


def _res(fp, fl, gp, gl, kept=-0.05, fpn=None, fln=None, gpn=None, gln=None):
    return {"f": {"p_all": fp, "lift_all": fl, "p_nogap": fp if fpn is None else fpn,
                  "lift_nogap": fl if fln is None else fln},
            "g": {"p_all": gp, "lift_all": gl, "p_nogap": gp if gpn is None else gpn,
                  "lift_nogap": gl if gln is None else gln, "kept_mean_all": kept}}


def test_verdict_pending_until_both_sets():
    assert fg.verdict({"A": _res(0.001, 0.02, 0.001, 0.02)})["status"] == "PENDING"


def test_verdict_pass_and_desk_rules():
    v = fg.verdict({"A": _res(0.001, 0.02, 0.001, 0.03, kept=-0.01),
                    "B": _res(0.002, 0.01, 0.003, 0.02, kept=-0.02)})
    assert v["ideas"]["f"] == {"status": "PASS", "role": "filter only", "desk_eligible": False,
                               "why_no_desk": v["ideas"]["f"]["why_no_desk"]}
    assert v["ideas"]["g"]["status"] == "PASS"
    assert v["ideas"]["g"]["desk_eligible"] is False      # kept trades still lose money
    v2 = fg.verdict({"A": _res(0.001, 0.02, 0.001, 0.03, kept=0.01),
                     "B": _res(0.002, 0.01, 0.003, 0.02, kept=0.02)})
    assert v2["ideas"]["g"]["desk_eligible"] is True


def test_verdict_holm_over_four_and_each_set_on_its_own():
    # raw p 0.02 everywhere -> Holm 0.08 -> nothing passes
    v = fg.verdict({"A": _res(0.02, 0.02, 0.02, 0.02), "B": _res(0.02, 0.02, 0.02, 0.02)})
    assert v["tests"]["f|A"]["holm"] == 0.08
    assert v["ideas"]["f"]["status"] == "FAIL" and v["ideas"]["g"]["status"] == "FAIL"
    # strong in A, negative lift in B -> FAIL
    v = fg.verdict({"A": _res(1e-6, 0.02, 1e-6, 0.02), "B": _res(1e-6, -0.01, 1e-6, 0.02)})
    assert v["ideas"]["f"]["status"] == "FAIL" and v["ideas"]["g"]["status"] == "PASS"


def test_verdict_needs_fuller_data_when_only_gap_dropped_passes():
    v = fg.verdict({"A": _res(0.3, 0.01, 0.3, 0.01, fpn=1e-5, gpn=0.4),
                    "B": _res(0.3, 0.01, 0.3, 0.01, fpn=1e-5, gpn=0.4)})
    assert v["ideas"]["f"]["status"] == "needs fuller data"
    assert v["ideas"]["g"]["status"] == "FAIL"


def test_check_set_ready(tmp_path):
    pf = tmp_path
    assert len(fg.check_set_ready("A", str(pf))) == 4
    for e in fg.set_epochs("A"):
        lo, hi = fg.epoch_bounds(e, e)
        (pf / f"trades/epoch={e}").mkdir(parents=True)
        (pf / f"blocks/epoch={e}").mkdir(parents=True)
        (pf / f"epoch-{e}.summary.json").write_text(json.dumps(
            {"parse_complete": True, "first_slot": lo, "last_slot": hi}))
    assert fg.check_set_ready("A", str(pf)) == []
    (pf / "epoch-1059.summary.json").write_text(json.dumps(
        {"parse_complete": False, "first_slot": 1059 * 432000, "last_slot": 1060 * 432000 - 1}))
    assert fg.check_set_ready("A", str(pf)) == ["epoch 1059: parse_complete is not true"]
