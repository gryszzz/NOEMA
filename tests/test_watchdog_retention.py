import gzip
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from noema import retention, watchdog


def test_prune_only_old_evidence(tmp_path):
    db = tmp_path / "cs.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE contract_observations (id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT, contract_id TEXT,"
              " observed_at TEXT, observation_hash TEXT UNIQUE, observation_json TEXT)")
    c.execute("CREATE TABLE live_balance_observations (id INTEGER PRIMARY KEY, observed_at TEXT)")
    now = datetime(2026, 10, 22, tzinfo=UTC)
    for i in range(1200):
        at = (now - timedelta(hours=48 - i * 0.04)).isoformat()
        c.execute("INSERT INTO contract_observations VALUES (NULL,'k','c',?,?,'{}')", (at, f"h{i}"))
        c.execute("INSERT INTO live_balance_observations VALUES (NULL,?)", (at,))
    c.commit()
    c.close()
    out = retention.prune_console_state(str(db), evidence_hours=24, now=now)
    c = sqlite3.connect(db)
    left = c.execute("SELECT MIN(observed_at), COUNT(*) FROM contract_observations").fetchone()
    assert left[0] >= (now - timedelta(hours=24)).isoformat()
    assert out["deleted"]["contract_observations"] == 1200 - left[1]
    assert c.execute("SELECT COUNT(*) FROM live_balance_observations").fetchone()[0] == 1200  # balances untouched


def test_missing_db_is_safe(tmp_path):
    assert retention.prune_console_state(str(tmp_path / "nope.db"))["error"] == "missing"


def test_rotate_logs_copytruncate(tmp_path):
    p = tmp_path / "x.log"
    p.write_bytes(b"a" * 2000)
    (tmp_path / "small.log").write_text("ok")
    assert watchdog.rotate_logs(str(tmp_path), max_bytes=1000) == ["x.log"]
    assert p.stat().st_size == 0
    with gzip.open(tmp_path / "x.log.1.gz") as g:
        assert g.read() == b"a" * 2000
    p.write_bytes(b"b" * 2000)
    watchdog.rotate_logs(str(tmp_path), max_bytes=1000)
    with gzip.open(tmp_path / "x.log.2.gz") as g:
        assert g.read() == b"a" * 2000


def test_reconcile_fixes_stale_pidfile(tmp_path, monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "zz-wd-test-marker"])
    try:
        monkeypatch.setattr(watchdog, "LOOPS", {"agent:treasury_updater": ("zz-wd-test-marker", 60, "equity_feed.log"),
                                                "agent:gone": ("zz-no-such-marker", 60, "gone.log")})
        (tmp_path / "equity_feed.pid").write_text("999999\n")
        (tmp_path / "gone.pid").write_text("999998\n")
        acts = watchdog.reconcile_pidfiles(str(tmp_path))
        assert acts == {"equity_feed.pid": "fixed", "gone.pid": "removed_dead"}
        assert int((tmp_path / "equity_feed.pid").read_text()) == proc.pid
        assert not (tmp_path / "gone.pid").exists()
    finally:
        proc.kill()


def test_pumpportal_low_balance(monkeypatch):
    monkeypatch.setenv("NOEMA_PUMPPORTAL_WALLET_ADDRESS", "Addr1111")
    assert watchdog.pumpportal_wallet_check(fetch=lambda a: 0.1)[0] is True
    monkeypatch.delenv("NOEMA_PUMPPORTAL_WARN_SOL", raising=False)
    assert watchdog.pumpportal_wallet_check(fetch=lambda a: 0.0308)[0] is True
    ok, msg = watchdog.pumpportal_wallet_check(fetch=lambda a: 0.022)
    assert not ok and msg.startswith("LOW")
    monkeypatch.setenv("NOEMA_PUMPPORTAL_WARN_SOL", "0.05")
    assert watchdog.pumpportal_wallet_check(fetch=lambda a: 0.0308)[0] is False
    monkeypatch.delenv("NOEMA_PUMPPORTAL_WARN_SOL")
    ok, msg = watchdog.pumpportal_wallet_check(fetch=lambda a: 0.01)
    assert not ok and "cutoff" in msg


def test_missing_secrets_names_only():
    assert watchdog.missing_secrets({k: "x" for k in watchdog.REQUIRED_SECRETS}) == []
    assert "HELIUS_API_KEY" in watchdog.missing_secrets({})


def test_disk_status(tmp_path):
    ok, msg = watchdog.disk_status(str(tmp_path), min_free_gb=0, max_used=1.0)
    assert ok and "GB free" in msg
    assert not watchdog.disk_status(str(tmp_path), min_free_gb=10**9)[0]


def test_step_isolates_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_HEARTBEAT_DIR", str(tmp_path))
    assert watchdog._step("system:x", lambda: 1 / 0, 60) is None
    assert os.path.exists(tmp_path / "system_x.json") or os.path.exists(tmp_path / "system:x.json")
