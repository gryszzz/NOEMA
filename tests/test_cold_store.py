import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pyarrow.dataset as ds
import pytest

from noema import cold_store as cs
from noema import pool_stream as ps
from noema import retention
from noema.ledger import ForecastLedger, stored_pair_payload


def test_payload_codec_round_trip_and_passthrough():
    big = json.dumps({"x": "a" * 5000, "n": [1, 2, 3]})
    enc = cs.encode_payload(big)
    assert isinstance(enc, bytes) and enc.startswith(cs.MAGIC) and len(enc) < len(big) / 5
    assert cs.decode_payload(enc) == big
    assert cs.decode_payload(memoryview(enc)) == big
    assert cs.encode_payload("{}") == "{}" and cs.decode_payload("{}") == "{}"
    assert cs.decode_payload(None) is None
    fill = json.dumps({"status": "paper_fill_simulated", "pad": "z" * 5000})
    assert stored_pair_payload(fill) == fill          # stays TEXT for the instr() prefilter


def _pool_db(path, n, received_at):
    c = ps.connect(str(path))
    rows = []
    for i in range(n):
        ts = int(received_at.timestamp()) + i
        rows.append((f"sig{i}", f"pool{i % 3}", 0, f"mint{i % 3}", "pump_amm", 100 + i, ts,
                     (received_at + timedelta(seconds=i)).isoformat(), "buy", 0.1 * i, 10.0 + i, None,
                     1e-7 * (i + 1), 30.0, 1e6, 80.0, 0.0, 0.0, "free_ws", "ok", 30.0, 2e-7, "vault+virtual"))
    c.executemany(ps.INSERT_TRADE, rows)
    c.commit()
    return c


def test_pool_trades_compaction_round_trip_no_row_loss(tmp_path):
    db = tmp_path / "pool_trades.db"
    old = datetime.now(UTC) - timedelta(days=2)
    c = _pool_db(db, 250, old)
    fresh = datetime.now(UTC).isoformat()
    c.execute(ps.INSERT_TRADE, ("hot", "pool0", 0, "mint0", "pump_amm", 9999, int(time.time()), fresh, "sell",
                                1.0, 1.0, "t", 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, "free_ws", "ok", None, None,
                                "vault_only_event"))
    c.commit()
    before = sorted(map(tuple, c.execute("SELECT * FROM pool_trades").fetchall()))
    res = ps.compact_sync(c, hot_h=24, max_rows=10**9)
    assert res["archived"] == 250 and not res["errors"]
    assert c.execute("SELECT COUNT(*) FROM pool_trades").fetchone()[0] == 1
    c.close()
    files = list((tmp_path / "cold" / "pool_trades" / "pool_trades").glob("date=*/part-*.parquet"))
    assert files and cs.manifest(ps.cold_root(str(db)), "pool_trades")["rows"] == 250
    rows = ps.read_trades(str(db))
    assert len(rows) == 251
    cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(pool_trades)")]
    assert sorted(tuple(r[k] for k in cols) for r in rows) == before     # every value survives
    assert {r["signature"] for r in ps.read_trades(str(db), mints=["mint1"])} == \
        {f"sig{i}" for i in range(250) if i % 3 == 1}
    since = int(old.timestamp()) + 200
    assert len(ps.read_trades(str(db), since_ts=since)) == 51


def test_pool_trades_reimport_after_compaction_is_deduped(tmp_path):
    db = tmp_path / "pool_trades.db"
    old = datetime.now(UTC) - timedelta(days=2)
    c = _pool_db(db, 40, old)
    ps.compact_sync(c, hot_h=24, max_rows=10**9)
    c.close()
    c = _pool_db(db, 40, old)     # e.g. a gap-fill importer re-inserting archived trades (INSERT OR IGNORE)
    assert len(ps.read_trades(str(db))) == 40
    ps.compact_sync(c, hot_h=24, max_rows=10**9)     # same deterministic parts are rewritten, not duplicated
    c.close()
    assert len(ps.read_trades(str(db))) == 40
    assert cs.manifest(ps.cold_root(str(db)), "pool_trades")["rows"] == 40


def test_compaction_never_deletes_when_parquet_write_fails(tmp_path, monkeypatch):
    db = tmp_path / "pool_trades.db"
    c = _pool_db(db, 30, datetime.now(UTC) - timedelta(days=2))

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(cs, "write_verified", boom)
    with pytest.raises(RuntimeError):
        ps.compact_sync(c, hot_h=24, max_rows=10**9)
    assert c.execute("SELECT COUNT(*) FROM pool_trades").fetchone()[0] == 30
    c.close()


def test_load_state_and_heartbeat_reader_only_need_hot_rows(tmp_path):
    db = tmp_path / "pool_trades.db"
    c = _pool_db(db, 10, datetime.now(UTC) - timedelta(days=3))
    ps.compact_sync(c, hot_h=24, max_rows=10**9)
    c.close()
    st = ps.State()
    ps.load_state(str(db), st)   # must not fail with an empty hot table
    assert c is not None


def _pair_observation(i, status="no_fill"):
    return {
        "observed_at": (datetime.now(UTC) - timedelta(hours=72 - i)).isoformat(),
        "canonical_identity": {"canonical_event_id": "ev", "canonical_proposition_id": f"p{i}",
                               "contracts": [{"venue": "kalshi", "contract_id": f"K{i}"},
                                             {"venue": "polymarket-us", "contract_id": f"P{i}"}],
                               "comparison": {"canonical_event_id": "ev", "canonical_proposition_id": f"p{i}",
                                              "semantic_match": "confirmed",
                                              "settlement_equivalence": "equivalent"}},
        "experiment_evaluation": {"paper_simulation": {"status": status}, "pad": "q" * 4000},
    }


def test_canonical_pair_observations_compressed_archived_and_readable(tmp_path, monkeypatch):
    from noema.ledger import ForecastLedger as FL
    path = tmp_path / "noema.console-state.db"
    led = FL(str(path))
    calls = []

    def fake_graph(self, *a, **k):
        calls.append(1)
    monkeypatch.setattr(FL, "_append_canonical_graph", fake_graph)
    obs = [_pair_observation(i, "paper_fill_simulated" if i == 0 else "no_fill") for i in range(6)]
    for o in obs:
        assert led.append_canonical_pair_observation(o)
    led.conn.close()
    c = sqlite3.connect(path)
    kinds = [r[0] for r in c.execute("SELECT typeof(observation_json) FROM canonical_pair_observations "
                                     "ORDER BY id")]
    assert kinds == ["text"] + ["blob"] * 5
    before = {h: cs.decode_payload(p) for h, p in
              c.execute("SELECT observation_hash, observation_json FROM canonical_pair_observations")}
    c.close()
    res = retention.prune_console_state(str(path), pair_hot_hours=48)
    # observed 72h..67h ago -> all older than 48h except none; the paper-fill row is never archived
    assert res["archived"]["canonical_pair_observations"] == 5
    c = sqlite3.connect(path)
    left = c.execute("SELECT observation_json FROM canonical_pair_observations").fetchall()
    assert len(left) == 1 and "paper_fill_simulated" in left[0][0]
    rows = cs.union_rows(c, cs.default_root(str(path)), "canonical_pair_observations", key=["observation_hash"],
                         payload_cols=["observation_json"])
    c.close()
    assert {r["observation_hash"]: r["observation_json"] for r in rows} == before
    one = cs.read_archive(cs.default_root(str(path)), "canonical_pair_observations",
                          filter_expr=ds.field("canonical_proposition_id") == "p3")
    assert json.loads(one[0]["observation_json"])["canonical_identity"]["comparison"]["canonical_proposition_id"] == "p3"


def test_recompress_existing_text_rows_is_lossless(tmp_path):
    path = tmp_path / "s.db"
    led = ForecastLedger(str(path))
    led.conn.close()
    c = sqlite3.connect(path)
    payloads = [json.dumps({"i": i, "pad": "w" * 3000}) for i in range(5)]
    payloads.append(json.dumps({"s": "paper_fill_simulated", "pad": "w" * 3000}))
    for i, p in enumerate(payloads):
        c.execute("INSERT INTO canonical_pair_observations (observed_at, canonical_event_id, "
                  "canonical_proposition_id, semantic_status, settlement_equivalence, observation_hash, "
                  "observation_json) VALUES (?,?,?,?,?,?,?)", ("2026-10-23T00:00:00+00:00", "e", f"p{i}",
                                                              "confirmed", "eq", f"h{i}", p))
    c.commit()
    assert cs.recompress_payloads(c, "canonical_pair_observations", "observation_json",
                                  keep_text_if="paper_fill_simulated") == 5
    got = [cs.decode_payload(r[0]) for r in c.execute("SELECT observation_json FROM canonical_pair_observations "
                                                      "ORDER BY id")]
    assert got == payloads
    assert c.execute("SELECT COUNT(*) FROM canonical_pair_observations WHERE instr(observation_json,"
                     "'paper_fill_simulated') > 0").fetchone()[0] == 1
    c.close()


def test_outcome_observations_archive(tmp_path):
    path = tmp_path / "noema.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE outcome_observations (evidence_hash TEXT PRIMARY KEY, venue TEXT NOT NULL, "
              "market_id TEXT NOT NULL, canonical_proposition_id TEXT, outcome_yes INTEGER NOT NULL, "
              "observed_at TEXT NOT NULL, resolved_at TEXT, source TEXT NOT NULL, source_url TEXT, "
              "raw_json TEXT NOT NULL)")
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    new = datetime.now(UTC).isoformat()
    c.executemany("INSERT INTO outcome_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                  [(f"h{i}", "kalshi:production", f"M{i}", None, i % 2, old if i < 7 else new, None, "api",
                    None, json.dumps({"i": i})) for i in range(10)])
    c.commit()
    c.close()
    assert retention.compact_noema_db(str(path)) == {"outcome_observations": 7}
    c = sqlite3.connect(path)
    rows = cs.union_rows(c, cs.default_root(str(path)), "outcome_observations", key=["evidence_hash"])
    c.close()
    assert sorted(r["evidence_hash"] for r in rows) == sorted(f"h{i}" for i in range(10))
    assert retention.compact_noema_db(str(path)) == {"outcome_observations": 0}
