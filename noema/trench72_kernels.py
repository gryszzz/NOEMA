"""Numba kernels for the 72 h on-chain trench study (research only; see noema/trench72.py).

Needs numpy + numba (not runtime deps of NOEMA). Nothing here trades, signs or reads keys.
All prices are virtual-reserve constant-product (pump.fun curve, or PumpSwap effective reserves)."""
from __future__ import annotations

import numpy as np
from numba import njit

FEE = 0.0125     # curve fee per side (protocol + creator), confirmed on-chain
BUF = 0.01       # extra slippage buffer per side
PRIO = 0.0005    # SOL per tx (priority + base)
V0 = 30e9        # initial virtual SOL (lamports)
CURVE = 85e9     # real SOL to complete (lamports)


@njit(cache=True)
def slot_time(blk_slot, blk_ts, s):
    i = np.searchsorted(blk_slot, s, side="right") - 1
    if i < 0:
        return blk_ts[0]
    return blk_ts[i]


@njit(cache=True)
def first_slot_at(blk_slot, blk_ts, t):
    i = np.searchsorted(blk_ts, t, side="left")
    if i >= blk_ts.shape[0]:
        return -1
    return blk_slot[i]


@njit(cache=True)
def last_le(slot, lo, hi, s):
    """Index of the last trade in [lo, hi) with slot <= s, or lo-1."""
    return lo + np.searchsorted(slot[lo:hi], s, side="right") - 1


@njit(cache=True)
def sim_one(lo, hi, slot, vs, vt, s_sig, L, size, cslot, end_slot, blk_slot, blk_ts,
            tmax, sl, l1r, l1f, l2r, l2f, trail, after, exitprog, fee, buf, prio):
    """Net return on SOL in for one paper trade, or nan when not executable / not fully observed.
    sl/trail/exitprog < 0 = off; l1f/l2f = 0 = no ladder level. Returns (ret, entry_slot, exit_slot)."""
    se = s_sig + L
    if se > end_slot:
        return np.nan, se, -1
    if cslot >= 0 and se >= cslot:
        return np.nan, se, -1
    j = last_le(slot, lo, hi, se)
    if j < lo:
        return np.nan, se, -1
    S0 = vs[j]
    T0 = vt[j]
    xnet = size * 1e9 * (1.0 - fee)
    tk = T0 - S0 * T0 / (S0 + xnet)
    hold = tk * (1.0 - buf)
    q0 = hold
    dS = xnet
    dT = tk
    pe = S0 / T0
    te = slot_time(blk_slot, blk_ts, se)
    s_time = first_slot_at(blk_slot, blk_ts, te + tmax)
    if s_time < 0:
        return np.nan, se, -1
    out = 0.0
    txs = 1
    peak = pe
    filled = 0
    k = j + 1
    last_exit = -1
    while k < hi and slot[k] < s_time and hold > 0:
        if cslot >= 0 and slot[k] > cslot:
            break
        p = vs[k] / vt[k]
        r = p / pe - 1.0
        peak = max(peak, p)
        x_slot = slot[k] + L
        sell = 0.0
        full = False
        lad = False
        if filled == 0 and l1f > 0 and r >= l1r:
            sell = min(hold, q0 * l1f)
            lad = True
        elif filled == 1 and l2f > 0 and r >= l2r:
            sell = min(hold, q0 * l2f)
            lad = True
        if not lad:
            armed = trail >= 0 and (filled > 0 or not after)
            hitp = exitprog >= 0 and (vs[k] - V0) / CURVE >= exitprog
            if (sl >= 0 and r <= -sl) or (armed and p <= peak * (1.0 - trail)) or hitp:
                sell = hold
                full = True
        if sell > 0:
            if x_slot > end_slot:
                return np.nan, se, -1
            fs = x_slot
            if cslot >= 0 and fs > cslot:
                fs = cslot
            f = last_le(slot, lo, hi, fs)
            S = vs[f] + dS
            T = vt[f] - dT
            g = S - S * T / (T + sell)
            out += g * (1.0 - fee) * (1.0 - buf)
            dS -= g
            dT -= sell
            hold -= sell
            txs += 1
            last_exit = x_slot
            if lad:
                filled += 1
                if hold <= q0 * 1e-9:
                    hold = 0.0
            if full:
                hold = 0.0
        k += 1
    if hold > 0:
        xs = s_time + L
        if cslot >= 0 and xs > cslot:
            xs = cslot
        elif xs > end_slot:
            return np.nan, se, -1
        f = last_le(slot, lo, hi, xs)
        S = vs[f] + dS
        T = vt[f] - dT
        g = S - S * T / (T + hold)
        out += g * (1.0 - fee) * (1.0 - buf)
        txs += 1
        last_exit = xs
    size_l = size * 1e9
    return (out - size_l - prio * 1e9 * txs) / size_l, se, last_exit


@njit(cache=True)
def sim_many(cidx, ssig, off, cslot_arr, slot, vs, vt, L, size, end_slot, blk_slot, blk_ts,
             tmax, sl, l1r, l1f, l2r, l2f, trail, after, exitprog, fee, buf, prio):
    n = cidx.shape[0]
    ret = np.empty(n)
    ent = np.empty(n, np.int64)
    ext = np.empty(n, np.int64)
    for i in range(n):
        c = cidx[i]
        r, a, b = sim_one(off[c], off[c + 1], slot, vs, vt, ssig[i], L, size, cslot_arr[c], end_slot,
                          blk_slot, blk_ts, tmax, sl, l1r, l1f, l2r, l2f, trail, after, exitprog,
                          fee, buf, prio)
        ret[i] = r
        ent[i] = a
        ext[i] = b
    return ret, ent, ext


@njit(cache=True)
def first_buys(off, slot, ts, isbuy, wid, iscr, cre_wid, t0, max_age, seen):
    """First buy per (wallet, coin) within max_age s of create (block time), creator excluded.
    `seen` is a scratch int64 array (len = n wallets) initialised to -1."""
    nc = off.shape[0] - 1
    cap = 0
    for c in range(nc):
        cap += off[c + 1] - off[c]
    oc = np.empty(cap, np.int64)
    os_ = np.empty(cap, np.int64)
    ow = np.empty(cap, np.int64)
    m = 0
    for c in range(nc):
        for k in range(off[c], off[c + 1]):
            if ts[k] - t0[c] > max_age:
                break
            if isbuy[k] == 0 or iscr[k] == 1 or wid[k] == cre_wid[c]:
                continue
            w = wid[k]
            if seen[w] == c:
                continue
            seen[w] = c
            oc[m] = c
            os_[m] = slot[k]
            ow[m] = w
            m += 1
    return oc[:m], os_[:m], ow[:m]


@njit(cache=True)
def b1_signals(off, slot, ts, t0, create_slot, age, min_n, blk_slot, blk_ts):
    """B1: coins with >= min_n trades with block time <= t0+age; signal = last slot with block time <= t0+age."""
    nc = off.shape[0] - 1
    oc = np.empty(nc, np.int64)
    os_ = np.empty(nc, np.int64)
    m = 0
    for c in range(nc):
        cnt = 0
        for k in range(off[c], off[c + 1]):
            if ts[k] > t0[c] + age:
                break
            cnt += 1
        if cnt >= min_n:
            s = first_slot_at(blk_slot, blk_ts, t0[c] + age + 1) - 1
            if s < 0:
                continue
            s = max(create_slot[c], s)
            oc[m] = c
            os_[m] = s
            m += 1
    return oc[:m], os_[:m]


@njit(cache=True)
def cross_signals(off, slot, vs, vt, thr_mc, cslot_arr):
    """First trade with mcap (SOL) >= thr; mcap = vs/vt*1e6 (lamports/raw -> SOL for 1e9 tokens)."""
    nc = off.shape[0] - 1
    oc = np.empty(nc, np.int64)
    os_ = np.empty(nc, np.int64)
    m = 0
    for c in range(nc):
        for k in range(off[c], off[c + 1]):
            if vs[k] / vt[k] * 1e6 >= thr_mc:
                oc[m] = c
                os_[m] = slot[k]
                m += 1
                break
    return oc[:m], os_[:m]


@njit(cache=True)
def manip_feats(cidx, ssig, off, slot, isbuy, tok, sol, wid, sigh, iscr, create_slot, cre_wid,
                lastside, flips, net, touched):
    """wash_max, atomic round-trip signatures and launch-block sniper share up to the signal slot."""
    n = cidx.shape[0]
    wash = np.zeros(n)
    atom = np.zeros(n, np.int64)
    snip = np.zeros(n)
    for i in range(n):
        c = cidx[i]
        lo = off[c]
        hi = off[c + 1]
        nt = 0
        bs = 0.0
        sb = 0.0
        prev_sig = np.uint64(0)
        prev_side = -1
        sig_has_rt = False
        for k in range(lo, hi):
            if slot[k] > ssig[i]:
                break
            w = wid[k]
            if lastside[w] == -2:
                touched[nt] = w
                nt += 1
                lastside[w] = -1
            if lastside[w] >= 0 and lastside[w] != isbuy[k]:
                flips[w] += 1
            lastside[w] = isbuy[k]
            net[w] += tok[k] if isbuy[k] == 1 else -tok[k]
            if k > lo and sigh[k] == prev_sig:
                if isbuy[k] != prev_side and not sig_has_rt:
                    atom[i] += 1
                    sig_has_rt = True
            else:
                sig_has_rt = False
            prev_sig = sigh[k]
            prev_side = isbuy[k]
            if isbuy[k] == 1:
                bs += sol[k]
                if slot[k] == create_slot[c] and iscr[k] == 0 and w != cre_wid[c]:
                    sb += sol[k]
        wm = 0.0
        for t in range(nt):
            w = touched[t]
            v = flips[w] / (abs(net[w]) + 1.0)
            wm = max(wm, v)
            lastside[w] = -2
            flips[w] = 0
            net[w] = 0.0
        wash[i] = wm
        snip[i] = sb / bs if bs > 0 else 0.0
    return wash, atom, snip


@njit(cache=True)
def max_prog_1h(off, ts, vs, t0):
    nc = off.shape[0] - 1
    out = np.zeros(nc)
    for c in range(nc):
        m = V0
        for k in range(off[c], off[c + 1]):
            if ts[k] > t0[c] + 3600:
                break
            m = max(m, vs[k])
        out[c] = (m - V0) / CURVE
    return out


@njit(cache=True)
def reentry_signals(off, slot, ts, isbuy, sol, tok, vs, wid, create_slot, t0, cre_wid, mode,
                    coh, hb, hs, cnt, touched):
    """(g) mode 0 = treatment (cohort holds <= 20 %), 1 = matched control (> 20 %), 2 = no inventory cond.
    Returns coin idx + signal slot for the first qualifying trade per coin."""
    nc = off.shape[0] - 1
    oc = np.empty(nc, np.int64)
    os_ = np.empty(nc, np.int64)
    m = 0
    for c in range(nc):
        lo = off[c]
        hi = off[c + 1]
        nt = 0
        cw = cre_wid[c]
        if cw >= 0:
            coh[cw] = 1
            touched[nt] = cw
            nt += 1
        bought = 0.0
        held = 0.0
        wlo = lo
        distinct = 0
        inflow = 0.0
        found = -1
        for k in range(lo, hi):
            w = wid[k]
            if slot[k] <= create_slot[c] + 4 and isbuy[k] == 1 and coh[w] == 0:
                coh[w] = 1
                touched[nt] = w
                nt += 1
            if coh[w] == 1:
                old = max(0.0, hb[w] - hs[w])
                if isbuy[k] == 1:
                    hb[w] += tok[k]
                    bought += tok[k]
                else:
                    hs[w] += tok[k]
                held += max(0.0, hb[w] - hs[w]) - old
            # 60 s window [ts-60, ts]
            inflow += sol[k] if isbuy[k] == 1 else -sol[k]
            if isbuy[k] == 1 and coh[w] == 0:
                if cnt[w] == 0:
                    distinct += 1
                cnt[w] += 1
            while ts[wlo] < ts[k] - 60:
                u = wid[wlo]
                inflow -= sol[wlo] if isbuy[wlo] == 1 else -sol[wlo]
                if isbuy[wlo] == 1 and coh[u] == 0:
                    cnt[u] -= 1
                    if cnt[u] == 0:
                        distinct -= 1
                wlo += 1
            age = ts[k] - t0[c]
            if age > 1800:
                break
            if found < 0 and age >= 60:
                prog = (vs[k] - V0) / CURVE
                if distinct >= 10 and inflow > 0 and prog >= 0.25 and prog <= 0.70:
                    inv = held / bought if bought > 0 else 0.0
                    ok = (mode == 2) or (mode == 0 and inv <= 0.2) or (mode == 1 and inv > 0.2)
                    if ok:
                        found = slot[k]
                        break
        # reset scratch
        for k2 in range(lo, hi):
            u = wid[k2]
            cnt[u] = 0
            hb[u] = 0.0
            hs[u] = 0.0
        for t in range(nt):
            coh[touched[t]] = 0
        if found >= 0:
            oc[m] = c
            os_[m] = found
            m += 1
    return oc[:m], os_[:m]
