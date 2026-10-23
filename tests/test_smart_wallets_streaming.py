"""The streaming smart_wallets engine must reproduce the old load-everything implementation exactly.

`_Legacy` below is a verbatim copy of the pre-streaming code paths (load_trades / build_positions / wallet_stats /
follow_signals / candidates); the fixture is a randomized multi-hour pump.fun history with vetted traders, snipers,
devs, bundles, partial sells, wallet_trades backfill rows and duplicates.
"""
import json
import math
import random
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

import pytest

from noema import meta_launch as ml
from noema import smart_wallets as sw

T0 = datetime(2026, 10, 20, 0, tzinfo=UTC).timestamp()
SPAN = 30 * 3600


class _Legacy:
    @staticmethod
    def load_trades(main_db, smart_db):
        rows, meta = {}, {}
        m = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
        for mint, at, creator in m.execute("SELECT mint, received_at, creator FROM launch_stream"):
            meta[mint] = {"created_at": sw._ts(at), "creator": creator}
        for sig, mint, at, side, sol, tok, trader, mc in m.execute(
                "SELECT signature, mint, received_at, side, sol, tokens, trader, market_cap_sol FROM launch_trades "
                "WHERE trader IS NOT NULL"):
            rows[(sig, trader)] = {"sig": sig.split(":")[0], "wallet": trader, "mint": mint, "t": sw._ts(at),
                                   "side": side, "sol": float(sol or 0), "tokens": float(tok or 0), "mcap": mc}
        m.close()
        s = sqlite3.connect(f"file:{smart_db}?mode=ro", uri=True)
        for sig, w, mint, at, side, sol, tok, mc in s.execute("SELECT * FROM wallet_trades"):
            rows.setdefault((sig, w), {"sig": sig, "wallet": w, "mint": mint, "t": sw._ts(at), "side": side,
                                       "sol": float(sol or 0), "tokens": float(tok or 0), "mcap": mc})
        s.close()
        return sorted(rows.values(), key=lambda r: r["t"]), meta

    @staticmethod
    def build_positions(trades, meta):
        by_sig = defaultdict(set)
        for t in trades:
            if t["side"] == "buy":
                by_sig[t["sig"]].add(t["wallet"])
        pos = {}
        for t in trades:
            k = (t["wallet"], t["mint"])
            p = pos.get(k)
            if p is None:
                if t["side"] != "buy":
                    continue
                mm = meta.get(t["mint"]) or {}
                age = t["t"] - mm["created_at"] if mm.get("created_at") else None
                p = pos[k] = {"wallet": t["wallet"], "mint": t["mint"], "entry_at": t["t"], "entry_mcap": t["mcap"],
                              "entry_age_s": age, "dev": bool(mm.get("creator")) and mm.get("creator") == t["wallet"],
                              "snipe": age is not None and age < sw.SNIPE_S, "bundled": len(by_sig[t["sig"]]) > 1,
                              "cost": 0.0, "bought": 0.0, "proceeds": 0.0, "sold": 0.0, "n_sells": 0,
                              "close_at": None}
            if p["close_at"] is not None:
                continue
            if t["side"] == "buy":
                p["cost"] += t["sol"] * (1 + sw.FEE)
                p["bought"] += t["tokens"]
                p["first_size_sol"] = p.get("first_size_sol", t["sol"])
            else:
                p["proceeds"] += t["sol"] * (1 - sw.FEE)
                p["sold"] += t["tokens"]
                p["n_sells"] += 1
                if p["bought"] > 0 and p["sold"] >= 0.95 * p["bought"]:
                    p["close_at"] = t["t"]
                    p["pnl_sol"] = p["proceeds"] - p["cost"]
                    p["ret"] = p["pnl_sol"] / p["cost"] if p["cost"] else 0.0
                    p["hold_s"] = t["t"] - p["entry_at"]
                    p["partial"] = p["n_sells"] > 1
        return list(pos.values())

    @staticmethod
    def wallet_stats(positions, asof):
        by = defaultdict(list)
        for p in positions:
            if p["entry_at"] < asof:
                by[p["wallet"]].append(p)
        out = {}
        for w, ps in by.items():
            closed = [p for p in ps if p["close_at"] is not None and p["close_at"] < asof]
            old = [p for p in ps if p["entry_at"] < asof - 7200]
            old_open = [p for p in old if p["close_at"] is None or p["close_at"] >= asof]
            pnl = [p["pnl_sol"] for p in closed]
            st = {"n_entries": len(ps), "n_closed": len(closed),
                  "win_rate": sum(x > 0 for x in pnl) / len(pnl) if pnl else None,
                  "pnl_sol": round(sum(pnl), 4), "pnl_ex_best": round(sum(pnl) - max(pnl), 4) if pnl else None,
                  "dev_share": sum(p["dev"] for p in ps) / len(ps),
                  "snipe_share": sum(p["snipe"] for p in ps) / len(ps),
                  "bundle_share": sum(p["bundled"] for p in ps) / len(ps),
                  "open_ratio": len(old_open) / len(old) if old else 0.0, "ci_lo": None}
            if len(pnl) >= sw.MIN_N:
                st["ci_lo"] = round(sw.bootstrap_lo(pnl), 5)
            st["vetted"], st["why"] = sw._vet(st)
            out[w] = st
        return out

    @staticmethod
    def follow_signals(trades, positions, meta, now):
        by_mint = defaultdict(list)
        for t in trades:
            if t.get("mcap"):
                by_mint[t["mint"]].append(t)
        vet_cache, out, seen = {}, [], set()
        for t in trades:
            if t["side"] != "buy" or t["mint"] in seen:
                continue
            mm = meta.get(t["mint"]) or {}
            if not mm.get("created_at") or t["t"] - mm["created_at"] > sw.MAX_AGE_S or mm.get("creator") == t["wallet"]:
                continue
            hour = int(t["t"] // 3600)
            if hour not in vet_cache:
                vet_cache[hour] = _Legacy.wallet_stats(positions, hour * 3600.0)
            st = vet_cache[hour].get(t["wallet"])
            if not st or not st["vetted"]:
                continue
            seen.add(t["mint"])
            later = [x for x in by_mint[t["mint"]] if x["t"] >= t["t"] + sw.LAT_S]
            if not later:
                continue
            e = later[0]
            if not (sw.MCAP_MIN <= e["mcap"] <= sw.MCAP_MAX):
                continue
            exit_px, why, exit_t = None, None, None
            for x in later[1:]:
                if x["t"] - e["t"] > sw.MAX_HOLD_S:
                    break
                r = x["mcap"] / e["mcap"] - 1
                if r >= sw.TP or r <= sw.SL or (x["wallet"] == t["wallet"] and x["side"] == "sell"):
                    exit_px, exit_t = x["mcap"], x["t"]
                    why = "tp" if r >= sw.TP else "sl" if r <= sw.SL else "wallet_sold"
                    break
            if exit_px is None:
                if now - e["t"] < sw.MAX_HOLD_S:
                    continue
                last = [x for x in later if x["t"] - e["t"] <= sw.MAX_HOLD_S][-1]
                exit_px, exit_t, why = last["mcap"], last["t"], "time"
            ret = exit_px / e["mcap"] - 1 - 2 * sw.COST_SIDE
            out.append({"id": f"sf:{t['mint']}", "at": sw._iso(e["t"]), "wallet": t["wallet"], "mint": t["mint"],
                        "entry_mcap": e["mcap"], "exit_mcap": exit_px, "exit": why,
                        "hold_s": round(exit_t - e["t"], 1), "ret": round(ret, 4),
                        "net": round(sw.NOTIONAL_USD * ret, 4),
                        "x": {"entry_age_s": round(t["t"] - mm["created_at"], 1), "entry_mcap": e["mcap"],
                              "wallet_ci_lo": st["ci_lo"], "wallet_n": st["n_closed"]}})
        return out

    @staticmethod
    def candidates(positions, k=sw.WATCH_CAP):
        cnt, bad = defaultdict(set), set()
        for p in positions:
            cnt[p["wallet"]].add(p["mint"])
            if p["dev"]:
                bad.add(p["wallet"])
        return sorted((w for w in cnt if w not in bad and len(cnt[w]) >= 5), key=lambda w: -len(cnt[w]))[:k]


def _iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat()


def _fixture(tmp_path, seed):
    """Randomized history: launches with a mcap random walk, noise traders, 6 skilled traders (become vetted),
    snipers, devs buying their own coin, bundled buys (shared base signature 'x:i'), partial sells, sells without a
    seen buy, mcap gaps, exact timestamp ties, and wallet_trades rows that duplicate launch rows / each other."""
    rng = random.Random(seed)
    main, smart = str(tmp_path / f"n{seed}.db"), str(tmp_path / f"s{seed}.db")
    m = sqlite3.connect(main)
    m.execute("CREATE TABLE launch_stream (mint TEXT PRIMARY KEY, received_at TEXT, creator TEXT)")
    m.execute("CREATE TABLE launch_trades (signature TEXT PRIMARY KEY, mint TEXT, received_at TEXT, side TEXT, "
              "sol REAL, tokens REAL, trader TEXT, market_cap_sol REAL)")
    m.execute("CREATE INDEX launch_trades_mint ON launch_trades(mint, received_at)")
    s = sw.connect(smart)
    rows, n = [], [0]

    def sig():
        n[0] += 1
        return f"s{n[0]}"

    noise = [f"N{i}" for i in range(40)]
    skilled = [f"S{i}" for i in range(6)]
    snipers = [f"P{i}" for i in range(4)]
    for k in range(260):
        mint, dev = f"M{k}", f"D{k % 30}"
        c0 = T0 + rng.uniform(0, SPAN)
        m.execute("INSERT INTO launch_stream VALUES (?,?,?)", (mint, _iso(c0), dev))
        drift = rng.choice([-0.002, 0.0, 0.001, 0.003])
        mc = lambda t, d=drift, c0=c0: round(30 * math.exp(d * (t - c0) + 0.3 * math.sin((t - c0) / 97)), 3)
        if rng.random() < 0.5:
            rows.append((sig(), mint, c0 + 0.5, "buy", 1.0, 100, dev, mc(c0 + 0.5)))
        for p in rng.sample(snipers, 2):
            t = c0 + rng.uniform(0.1, 1.9)
            rows.append((sig(), mint, t, "buy", 0.5, 50, p, mc(t)))
            if rng.random() < 0.7:
                t2 = t + rng.uniform(5, 300)
                rows.append((sig(), mint, t2, "sell", 0.5 * mc(t2) / mc(t), 50, p, mc(t2)))
        if rng.random() < 0.3:  # bundle: two wallets in one tx (launch rows carry 'sig:idx')
            b, t = sig(), c0 + rng.uniform(3, 60)
            for j, w in enumerate(rng.sample(noise, 2)):
                rows.append((f"{b}:{j}", mint, t, "buy", 0.3, 30, w, mc(t)))
        for _ in range(rng.randint(5, 40)):
            w, t = rng.choice(noise), c0 + rng.uniform(2, 2400)
            side = rng.choice(["buy", "buy", "sell"])
            rows.append((sig(), mint, t, side, rng.uniform(0.05, 2), rng.uniform(5, 200), w,
                         None if rng.random() < 0.05 else mc(t)))
        for w in skilled:
            if rng.random() < 0.35:
                t, sz = c0 + rng.uniform(5, 600), rng.uniform(0.2, 2)
                rows.append((sig(), mint, t, "buy", sz, 100, w, mc(t)))
                tt = t + 1 if rng.random() < 0.3 else t
                if tt != t:
                    rows.append((sig(), mint, tt, "buy", sz / 2, 50, w, mc(tt)))  # top-up
                won = rng.random() < 0.8
                ts = tt + rng.uniform(30, 900)
                px = sz * (1.6 if won else 0.8)
                if rng.random() < 0.3:  # partial exit then the rest
                    rows.append((sig(), mint, ts, "sell", px / 2, 60, w, mc(ts)))
                    ts += rng.uniform(5, 100)
                    rows.append((sig(), mint, ts, "sell", px, 100, w, mc(ts)))
                elif rng.random() < 0.9:
                    rows.append((sig(), mint, ts, "sell", px * 1.5, 150, w, mc(ts)))
        if rng.random() < 0.1:
            rows.append((sig(), mint, c0 + 30, "sell", 1.0, 10, "Z", mc(c0 + 30)))  # sell w/o buy
    # exact-time ties
    for i in range(0, len(rows) - 1, 37):
        a = list(rows[i + 1])
        a[2] = rows[i][2]
        rows[i + 1] = tuple(a)
    m.executemany("INSERT INTO launch_trades VALUES (?,?,?,?,?,?,?,?)",
                  [(r[0], r[1], _iso(r[2]), r[3], r[4], r[5], r[6], r[7]) for r in rows])
    m.execute("INSERT INTO launch_trades VALUES ('anon','M1',?, 'buy', 1, 1, NULL, 30)", (_iso(T0 + 50),))
    m.commit()
    m.close()
    # wallet_trades: dupes of launch rows (must be ignored), backfilled history incl. unknown mints, inner dupes
    wt = []
    for r in rng.sample(rows, 200):
        if ":" not in r[0]:
            wt.append((r[0], r[6], r[1], _iso(r[2]), r[3], r[4] * 3, r[5], r[7]))
    for w in skilled[:3] + noise[:3]:
        for i in range(10):
            t = T0 - 3600 + i * 300
            b, sl = sig(), sig()
            wt.append((b, w, f"OLD{w}{i}", _iso(int(t)), "buy", 1.0, 100, 30.0))
            wt.append((sl, w, f"OLD{w}{i}", _iso(int(t + 120)), "sell", 1.4, 100, 40.0))
            if i % 4 == 0:
                wt.append((b, w, f"ALT{w}{i}", _iso(int(t + 5)), "buy", 9.0, 100, 30.0))  # same (sig, wallet): dupe
    s.executemany("INSERT OR IGNORE INTO wallet_trades VALUES (?,?,?,?,?,?,?,?)", wt)
    s.commit()
    s.close()
    return main, smart


def _norm(x):
    return json.loads(json.dumps(x, sort_keys=True))


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("now_off", [SPAN + 4 * 3600, SPAN + 600])
def test_streaming_matches_full_history(tmp_path, seed, now_off):
    main, smart = _fixture(tmp_path, seed)
    now = T0 + now_off
    tr, meta = _Legacy.load_trades(main, smart)
    pos = _Legacy.build_positions(tr, meta)
    st_old = _Legacy.wallet_stats(pos, now)
    sig_old = _Legacy.follow_signals(tr, pos, meta, now)

    assert sw.load_trades(main, smart)[0] == tr
    sc = sw.Scan(main, smart, now)
    assert sc.n_trades == len(tr)
    assert [p.as_dict() for p in sc.positions] == pos
    st_new = sw.wallet_stats(sc.positions, now, lazy_ci=True)
    vet_old = {w for w, s in st_old.items() if s["vetted"]}
    assert vet_old and {w for w, s in st_new.items() if s["vetted"]} == vet_old
    for w, s in st_old.items():  # identical stats; lazy ci_lo only skipped where it cannot change the verdict
        a = dict(st_new[w])
        if a["ci_lo"] is None and not s["vetted"]:
            a["ci_lo"] = s["ci_lo"]
        assert a == s, w
    assert sw.wallet_stats(sc.positions, now) == st_old  # eager mode: byte-identical
    assert len(sig_old) >= 3 and sc.signals == sig_old
    assert sw.follow_signals(tr, sw.build_positions(tr, meta), meta, now) == sig_old
    assert sw.candidates(sc.positions) == _Legacy.candidates(pos)
    assert sc.recent == [t for t in tr if t["t"] >= now - 1800]


def test_cycle_report_matches_full_history(tmp_path, monkeypatch):
    main, smart = _fixture(tmp_path, 4)
    now = T0 + SPAN + 4 * 3600
    monkeypatch.setattr(sw.time, "time", lambda: now)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    tr, meta = _Legacy.load_trades(main, smart)
    pos = _Legacy.build_positions(tr, meta)
    st = _Legacy.wallet_stats(pos, now)
    vetted = {w for w, s in st.items() if s["vetted"]}
    sig = _Legacy.follow_signals(tr, pos, meta, now)
    av = str(tmp_path / "av.db")
    rep = sw.cycle(main, smart, av)
    assert rep["trades"] == len(tr) and rep["positions"] == len(pos) and rep["wallets"] == len(st)
    assert rep["closed_positions"] == sum(p["close_at"] is not None for p in pos)
    assert {v["wallet"] for v in rep["vetted"]} == set(sorted(vetted, key=lambda w: -(st[w]["ci_lo"] or 0))[:25])
    assert _norm(rep["vetted"]) == _norm(sorted(({"wallet": w, **st[w]} for w in vetted),
                                                key=lambda r: -(r["ci_lo"] or 0))[:25])
    assert rep["rejections"] == sw._rej_counts(st)
    assert _norm(rep["patterns_vetted"]) == _norm(sw.patterns(pos, vetted))
    assert _norm(rep["patterns_all_closed"]) == _norm(sw.patterns(pos, set(st)))
    assert rep["smart_follow"]["n"] == len(sig) and _norm(rep["smart_follow"]["recent"]) == _norm(sig[-10:])
    w = sw.load_watch(smart)
    assert {k for k, v in w.items() if v == "vetted"} == vetted
    a = sqlite3.connect(av)
    assert {r[0] for r in a.execute("SELECT id FROM paper_outcomes WHERE kind='smart_follow'")} == {s["id"] for s in sig}
    a.close()


def test_meta_smart_feats_reads_published_vetted_set(tmp_path, monkeypatch):
    main, smart = _fixture(tmp_path, 5)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    now = T0 + SPAN + 4 * 3600
    monkeypatch.setattr(sw.time, "time", lambda: now)
    sw.cycle(main, smart, None)
    monkeypatch.setenv("NOEMA_SMART_DB", smart)
    monkeypatch.setattr(ml, "_SMART", {})
    vet = {k for k, v in sw.load_watch(smart).items() if v == "vetted"}
    tr, meta = _Legacy.load_trades(main, smart)
    pos = _Legacy.build_positions(tr, meta)
    vstats = {w: {"vetted": True} for w in vet}
    c = ml._ro(main)
    hits = 0
    for mint in [f"M{k}" for k in range(260)]:
        lt = [p for p in pos if p["mint"] == mint and not p["mint"].startswith("OLD")]
        exp = sw.smart_features(lt, mint, now, vstats)
        got = ml._smart_feats(mint, c, datetime.fromtimestamp(now, UTC))
        assert got == exp, mint
        hits += got["smart_buyers"] > 0
    c.close()
    assert hits > 0
