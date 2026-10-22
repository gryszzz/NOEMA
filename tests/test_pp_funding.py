from noema import launch_stream as ls
from noema import live_feed as lf


def test_funding_error_detection():
    assert ls._is_funding_error("only available when connecting with an API key funded with at least 0.02 SOL")
    assert ls._is_funding_error("Minimum balance not met for PumpSwap websocket data")
    assert not ls._is_funding_error('{"txType":"buy"}')


def test_note_clears_on_accepted(tmp_path, monkeypatch):
    log = tmp_path / "l.log"
    log.write_text("pumpportal msg Minimum balance not met for PumpSwap websocket data\n")
    monkeypatch.setenv("NOEMA_LAUNCH_LOG", str(log))
    assert lf._pumpportal_note()
    log.write_text(log.read_text() + "pumpportal keyed trades accepted\n")
    assert lf._pumpportal_note() is None
