"""QA regression: mature_paper_pairs streams only paper-filled observations (was a ~2 GB fetchall)."""
import inspect

from noema import cross_venue_experiment


def test_mature_paper_pairs_prefilters_and_streams() -> None:
    src = inspect.getsource(cross_venue_experiment.mature_paper_pairs)
    assert "instr(observation_json, 'paper_fill_simulated') > 0" in src
    assert "FROM canonical_pair_observations ORDER BY id\"\n        ).fetchall()" not in src


def test_looking_panel_renders_null_features_as_dash() -> None:
    from pathlib import Path
    js = Path(cross_venue_experiment.__file__).with_name("static").joinpath("live-panels.js").read_text()
    assert 'v==null||(typeof v==="number"&&!isFinite(v))?"—"' in js
