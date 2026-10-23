import os
import sqlite3
from datetime import UTC, datetime, timedelta

from noema import proven_sizing as ps
from noema import suspend_guard as sg
from noema.trench_desks import CostModel, Mark, PaperDeskEngine, TrenchDeskStore, desk_overview

HEADER = "start_et,end_et,start_utc,end_utc,duration_s,detection,note\n"


def _iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path, windows):
    with open(path, "w") as f:
        f.write(HEADER)
        f.writelines(f",,{_iso(start)},{_iso(end)},{int((end - start).total_seconds())},loop_step,test\n" for start, end in windows)
    # bump mtime so the cache reloads even within one second
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 1))


def test_missing_file_is_reported_not_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_SUSPENDS_CSV", str(tmp_path / "nope.csv"))
    assert sg.windows() == []
    assert sg.status()["status"] == "missing"
    assert sg.straddle("2026-10-23T05:00:00Z", "2026-10-23T06:00:00Z") is None


def test_straddle_overlap_padding_and_formats(tmp_path, monkeypatch):
    path = tmp_path / "s.csv"
    monkeypatch.setenv("NOEMA_SUSPENDS_CSV", str(path))
    t0 = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)
    _write(path, [(t0, t0 + timedelta(minutes=10))])
    assert sg.status()["windows"] == 1
    # fully across, partially overlapping, ET offset input, epoch seconds input
    assert sg.straddle(t0 - timedelta(hours=1), t0 + timedelta(hours=1), pad_s=0)
    assert sg.straddle("2026-10-23T01:05:00-04:00", "2026-10-23T01:30:00-04:00", pad_s=0)
    assert sg.straddle((t0 + timedelta(minutes=5)).timestamp(), None, pad_s=0)  # still open
    # entirely before / after: clean without padding, invalid inside the pad
    before = (t0 - timedelta(minutes=30), t0 - timedelta(seconds=30))
    assert sg.straddle(*before, pad_s=0) is None
    assert sg.straddle(*before, pad_s=60) is not None
    after = (t0 + timedelta(minutes=11), t0 + timedelta(minutes=20))
    assert sg.straddle(*after, pad_s=0) is None
    assert "box suspend" in sg.void_reason(*before, pad_s=60)


def _engine(tmp_path):
    store = TrenchDeskStore(str(tmp_path / "d.db"))
    store.ensure_desks(5, 500.0)
    return store, PaperDeskEngine(store, costs=CostModel(network_fee_usd=0.0))


def _desk(store):
    return store.conn.execute("SELECT * FROM trench_desks WHERE desk_id='vet-g1'").fetchone()


def test_trade_open_across_suspend_is_void_not_win(tmp_path, monkeypatch):
    path = tmp_path / "s.csv"
    monkeypatch.setenv("NOEMA_SUSPENDS_CSV", str(path))
    store, engine = _engine(tmp_path)
    assert engine.try_enter("vet-g1", "M", "W", Mark(1.0, 1e9, "t"), "r")
    now = datetime.now(UTC)
    _write(path, [(now - timedelta(seconds=30), now + timedelta(seconds=30))])
    engine.manage_positions("vet-g1", {"M": Mark(5.0, 1e9, "t")})  # big up move -> full exit
    engine.manage_positions("vet-g1", {"M": Mark(0.1, 1e9, "t")})
    closed = store.conn.execute("SELECT * FROM trench_desk_positions WHERE status='closed'").fetchone()
    assert closed is not None and closed["scored_as"] == "void"
    assert "box suspend" in closed["void_reason"]
    desk = _desk(store)
    assert desk["wins"] == 0 and desk["losses"] == 0 and desk["consecutive_losses"] == 0
    events = [r["event"] for r in store.conn.execute("SELECT event FROM trench_desk_events")]
    assert "paper_trade_voided_suspend" in events
    engine.commit()
    ov = desk_overview(str(tmp_path / "d.db"))
    vet = next(d for d in ov["desks"] if d["desk_id"] == "vet-g1")
    assert vet["voided_suspend"] == 1
    assert ov["suspend_guard"]["windows"] == 1


def test_late_logged_window_retro_voids_and_reverses_score(tmp_path, monkeypatch):
    path = tmp_path / "s.csv"
    monkeypatch.setenv("NOEMA_SUSPENDS_CSV", str(path))
    store, engine = _engine(tmp_path)
    for mint in ("A", "B"):
        engine.try_enter("vet-g1", mint, mint, Mark(1.0, 1e9, "t"), "r")
    engine.manage_positions("vet-g1", {"A": Mark(0.5, 1e9, "t"), "B": Mark(0.5, 1e9, "t")})
    assert _desk(store)["losses"] == 2 and _desk(store)["consecutive_losses"] == 2
    assert engine.void_suspended() == 0  # no windows yet
    now = datetime.now(UTC)
    _write(path, [(now - timedelta(seconds=20), now - timedelta(seconds=10))])  # retro_logs row lands late
    assert engine.void_suspended() == 2
    desk = _desk(store)
    assert desk["losses"] == 0 and desk["consecutive_losses"] == 0
    assert engine.fire_if_needed("vet-g1", {}) is None  # voided losses can't fire the desk
    assert engine.void_suspended() == 0  # idempotent


def test_clean_trades_still_score(tmp_path, monkeypatch):
    path = tmp_path / "s.csv"
    monkeypatch.setenv("NOEMA_SUSPENDS_CSV", str(path))
    _write(path, [(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 0, 10, tzinfo=UTC))])
    store, engine = _engine(tmp_path)
    engine.try_enter("vet-g1", "A", "A", Mark(1.0, 1e9, "t"), "r")
    engine.manage_positions("vet-g1", {"A": Mark(0.5, 1e9, "t")})
    assert _desk(store)["losses"] == 1
    assert store.conn.execute("SELECT scored_as FROM trench_desk_positions").fetchone()[0] == "loss"


def test_proven_sizing_drops_suspend_straddling_evidence(tmp_path, monkeypatch):
    path = tmp_path / "s.csv"
    monkeypatch.setenv("NOEMA_SUSPENDS_CSV", str(path))
    monkeypatch.setenv("NOEMA_PROVEN_MIN_N", "30")
    t0 = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)
    _write(path, [(t0, t0 + timedelta(minutes=10))])
    feed = tmp_path / "f.db"
    c = sqlite3.connect(feed)
    c.execute("""CREATE TABLE opp_items (id TEXT PRIMARY KEY, source TEXT, tier INTEGER, title TEXT, venue TEXT,
        ref TEXT, first_seen TEXT, last_seen TEXT, source_at TEXT, price_kind TEXT, ref_price REAL, last_price REAL,
        edge REAL, score REAL, reason TEXT, url TEXT, status TEXT, expired_at TEXT, payload_json TEXT)""")
    for i in range(40):  # 40 winners, but 20 were "measured" across the suspend
        seen = t0 - timedelta(minutes=30) if i < 20 else t0 + timedelta(hours=2)
        c.execute("INSERT INTO opp_items (id, source, reason, price_kind, ref_price, last_price, status,"
                  " payload_json, first_seen, expired_at) VALUES (?,?,?,?,?,?, 'expired', '{}', ?, ?)",
                  (str(i), "alpha", "rug screens pass", "token", 1.0, 1.10 + (i % 3) * 0.01,
                   _iso(seen), _iso(seen + timedelta(hours=1))))
    c.commit()
    c.close()
    b = ps.bucket_scores(str(feed))["alpha:rug screens pass"]
    assert b["voided_suspend"] == 20 and b["n"] == 20
    assert not b["proven"]  # 20 < 30: suspend-era wins can't size it up
