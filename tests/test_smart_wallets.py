from noema import smart_wallets as sw
from noema.launch_stream import EXTEND, WATCH, handle_rpc_logs


def _t(w, mint, t, side, sol, tok, mcap=30.0, sig=None):
    return {"sig": sig or f"{w}{mint}{t}{side}", "wallet": w, "mint": mint, "t": t, "side": side, "sol": sol,
            "tokens": tok, "mcap": mcap}


def _winner(w, n, t0=0.0, win=True):
    out, meta = [], {}
    for i in range(n):
        m = f"m{w}{i}"
        meta[m] = {"created_at": t0 + i * 100, "creator": "dev"}
        out.append(_t(w, m, t0 + i * 100 + 10, "buy", 1.0, 100))
        out.append(_t(w, m, t0 + i * 100 + 60, "sell", (1.5 if (win or i % 2) else 0.5), 100))
    return out, meta


def test_positions_and_vetting_no_lookahead():
    tr, meta = _winner("A", 12)
    pos = sw.build_positions(tr, meta)
    assert all(p["close_at"] for p in pos) and abs(pos[0]["pnl_sol"] - (1.5 * (1 - sw.FEE) - 1 * (1 + sw.FEE))) < 1e-9
    st = sw.wallet_stats(pos, 1e9)
    assert st["A"]["vetted"], st["A"]
    early = sw.wallet_stats(pos, 500)  # only ~5 closes known at t=500 -> not vetted yet
    assert not early["A"]["vetted"] and early["A"]["n_closed"] < sw.MIN_N


def test_filters_dev_sniper_lucky():
    tr, meta = _winner("D", 12)
    for m in meta.values():
        m["creator"] = "D"
    assert sw.wallet_stats(sw.build_positions(tr, meta), 1e9)["D"]["why"] == "dev/creator wallet"
    tr, meta = _winner("S", 12)
    tr = [dict(x, t=x["t"] - 9.5) if x["side"] == "buy" else x for x in tr]
    assert sw.wallet_stats(sw.build_positions(tr, meta), 1e9)["S"]["why"].startswith("sniper")
    out, meta = [], {}
    for i in range(12):  # 11 small losers + one huge winner
        m = f"L{i}"
        meta[m] = {"created_at": i * 100, "creator": "x"}
        out += [_t("L", m, i * 100 + 10, "buy", 1, 100), _t("L", m, i * 100 + 50, "sell", 50 if i == 0 else 0.9, 100)]
    assert not sw.wallet_stats(sw.build_positions(out, meta), 1e9)["L"]["vetted"]


def test_follow_signal_uses_prior_vetting_and_costs():
    tr, meta = _winner("A", 12)
    t0 = 3 * 3600.0
    meta["NEW"] = {"created_at": t0, "creator": "dev"}
    tr += [_t("A", "NEW", t0 + 20, "buy", 1, 100, 30), _t("Z", "NEW", t0 + 23, "buy", 1, 100, 30),
           _t("Z", "NEW", t0 + 60, "buy", 1, 100, 45)]
    pos = sw.build_positions(tr, meta)
    sig = sw.follow_signals(tr, pos, meta, now=t0 + 5000)
    assert len(sig) == 1 and sig[0]["exit"] == "tp" and abs(sig[0]["ret"] - (0.5 - 2 * sw.COST_SIDE)) < 1e-9
    assert sw.smart_features(pos, "NEW", t0 + 30)["smart_buyers"] == 1.0


def test_watch_extends_follow():
    import base64
    import struct

    from noema.launch_stream import b58  # noqa: F401
    WATCH.clear()
    EXTEND.clear()
    user = bytes(range(32))
    from noema.launch_stream import b58 as enc
    WATCH.add(enc(user))
    import hashlib
    disc = hashlib.sha256(b"event:TradeEvent").digest()[:8]
    data = disc + bytes(32) + struct.pack("<QQ?", 10**9, 10**6, True) + user + struct.pack("<qQQ", 0, 30 * 10**9, 10**12)

    class C:
        rows: list = []  # noqa: RUF012

        def execute(self, q, a=()):
            self.rows.append(a)

            class R:
                rowcount = 1
            return R()
    n = handle_rpc_logs(C(), "sigX", ["Program data: " + base64.b64encode(data).decode()], follow={}, now=1000.0)
    assert n == 1 and EXTEND and next(iter(EXTEND.values())) == 1000.0 + 1800
    WATCH.clear()
    EXTEND.clear()
