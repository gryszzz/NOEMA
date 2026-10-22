"""Lightweight supervisor for the paper runtime (no secrets printed, never touches live/owner flags).

Every cycle (default 60 s), each step isolated so one failure can't stop the others:
  * re-run start.sh (idempotent: it only launches loops whose process is missing) -> dead loops come back;
  * reconcile *.pid files with the real process (fixes stale pids, e.g. the old treasury-updater bug);
  * rotate oversized logs (copytruncate + gzip, keep N) so logs can't fill the disk;
  * disk / DB-size / secret-presence / runtime-venv / PumpPortal key-wallet balance heartbeats;
  * evidence-table retention every ``retention_every_s``.
All results land as explicit heartbeats (``system:*``, ``wallet:pumpportal_key``) on the header chip and heartbeats.json.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import time
import urllib.request
from typing import Any

from .heartbeats import LOOPS, RUN_DIR, beat

REQUIRED_SECRETS = ("HELIUS_API_KEY", "PUMPPORTAL_API_KEY", "KALSHI_API_KEY_ID", "POLYMARKET_US_KEY_ID",
                    "POLYMARKET_US_SECRET_KEY", "OPENAI_API_KEY")
PUMPPORTAL_CUTOFF_SOL = 0.02


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pids_for(marker: str) -> list[int]:
    out = []
    for p in os.listdir("/proc"):
        if p.isdigit() and int(p) != os.getpid():
            try:
                with open(f"/proc/{p}/cmdline", "rb") as f:
                    if marker in f.read().replace(b"\0", b" ").decode(errors="replace"):
                        out.append(int(p))
            except OSError:
                continue
    return sorted(out)


def reconcile_pidfiles(run_dir: str = RUN_DIR) -> dict[str, str]:
    """Point each loop's pid file at its live process (or drop it when none runs). Returns {pidfile: action}."""
    actions: dict[str, str] = {}
    for marker, _cad, log in LOOPS.values():
        pf = os.path.join(run_dir, log.rsplit(".", 1)[0] + ".pid")
        live = _pids_for(marker)
        cur = None
        try:
            with open(pf) as f:
                cur = int(f.read().strip())
        except (OSError, ValueError):
            pass
        if cur is not None and _pid_alive(cur) and cur in live:
            continue
        if live:
            with open(pf, "w") as f:
                f.write(f"{live[0]}\n")
            actions[os.path.basename(pf)] = "fixed" if cur is not None else "created"
        elif cur is not None:
            os.remove(pf)
            actions[os.path.basename(pf)] = "removed_dead"
    return actions


def rotate_logs(run_dir: str = RUN_DIR, max_bytes: int = 50 * 2**20, keep: int = 2) -> list[str]:
    """copytruncate (writers keep their O_APPEND fd) + gzip; keep ``keep`` generations."""
    rotated = []
    for fn in sorted(os.listdir(run_dir)):
        p = os.path.join(run_dir, fn)
        if not fn.endswith(".log") or not os.path.isfile(p) or os.path.getsize(p) < max_bytes:
            continue
        for i in range(keep, 1, -1):
            older = f"{p}.{i - 1}.gz"
            if os.path.exists(older):
                os.replace(older, f"{p}.{i}.gz")
        with open(p, "rb") as src, gzip.open(f"{p}.1.gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        with open(p, "r+b") as f:
            f.truncate(0)
        rotated.append(fn)
    return rotated


def disk_status(path: str, min_free_gb: float = 15.0, max_used: float = 0.90) -> tuple[bool, str]:
    u = shutil.disk_usage(path)
    free_gb, used = u.free / 2**30, u.used / u.total
    ok = free_gb >= min_free_gb and used <= max_used
    return ok, f"{free_gb:.0f} GB free ({used:.0%} used)"


def missing_secrets(env: dict[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    return [k for k in REQUIRED_SECRETS if not env.get(k)]


def sol_balance(address: str, timeout: float = 10.0) -> float:
    key = os.getenv("HELIUS_API_KEY")
    url = f"https://mainnet.helius-rpc.com/?api-key={key}" if key else "https://api.mainnet-beta.solana.com"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return int(json.load(r)["result"]["value"]) / 1e9


def pumpportal_wallet_check(warn_sol: float = 0.035, fetch: Any = sol_balance) -> tuple[bool, str]:
    addr = os.getenv("NOEMA_PUMPPORTAL_WALLET_ADDRESS")
    if not addr:
        return False, "set NOEMA_PUMPPORTAL_WALLET_ADDRESS (public address) to watch the key wallet balance"
    bal = fetch(addr)
    if bal < PUMPPORTAL_CUTOFF_SOL:
        return False, f"{bal:.4f} SOL - below PumpPortal's 0.02 SOL cutoff, keyed trades will stop"
    if bal < warn_sol:
        return False, f"LOW {bal:.4f} SOL - top up before it hits 0.02 SOL"
    return True, f"{bal:.4f} SOL"


def _step(name: str, fn: Any, cadence: float) -> Any:
    try:
        res = fn()
        if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], bool):
            beat(name, res[0], None if res[0] else res[1], cadence_s=cadence)
        else:
            beat(name, True, cadence_s=cadence)
        return res
    except Exception as exc:  # noqa: BLE001 - one broken step must not stop the supervisor
        beat(name, False, f"{type(exc).__name__}: {exc}", cadence_s=cadence)
        return None


def cycle(args: argparse.Namespace, state: dict[str, float]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    cad = args.interval
    if args.start_sh and os.path.exists(args.start_sh):
        out["start"] = _step("system:supervisor", lambda: subprocess.run(
            ["bash", args.start_sh], timeout=120, capture_output=True, check=False).returncode == 0 or
            (False, "start.sh exited non-zero"), cad)
    out["pids"] = _step("system:pidfiles", lambda: reconcile_pidfiles(args.run_dir), cad)
    out["logs"] = _step("system:log_rotation", lambda: rotate_logs(args.run_dir, int(args.max_log_mb * 2**20)), cad)
    out["disk"] = _step("system:disk", lambda: disk_status(args.data_dir), cad)
    m = missing_secrets()
    out["secrets"] = _step("system:secrets", lambda: (not m, "missing env: " + ",".join(m)), cad)
    out["venv"] = _step("system:runtime_venv", lambda: (os.path.exists(args.python),
                                                           f"{args.python} missing - recreate the venv"), cad)

    def db_sizes() -> tuple[bool, str]:
        big = {f: os.path.getsize(os.path.join(args.data_dir, f)) / 2**30 for f in os.listdir(args.data_dir)
               if f.endswith(".db")}
        worst = max(big.items(), key=lambda kv: kv[1]) if big else ("-", 0.0)
        return worst[1] < args.max_db_gb, f"largest {worst[0]} {worst[1]:.1f} GB"
    out["db"] = _step("system:db_size", db_sizes, cad)
    if time.time() - state.get("pp", 0) >= 300:
        state["pp"] = time.time()
        if os.getenv("NOEMA_PUMPPORTAL_WALLET_ADDRESS"):
            out["pumpportal"] = _step("wallet:pumpportal_key", pumpportal_wallet_check, 300)
        else:  # stream-side beat still flags the 0.02 SOL cutoff; this asks for the address for early warning
            beat("system:pumpportal_balance_watch", False, "set NOEMA_PUMPPORTAL_WALLET_ADDRESS for low-balance"
                 " warnings", cadence_s=300)
    if time.time() - state.get("ret", 0) >= args.retention_every_s:
        state["ret"] = time.time()
        from .retention import prune_console_state
        out["retention"] = _step("system:retention", lambda: prune_console_state(
            os.path.join(args.data_dir, "noema.console-state.db")), args.retention_every_s)
    beat("system:watchdog", True, cadence_s=cad)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NOEMA paper-runtime watchdog")
    ap.add_argument("--run-dir", default=RUN_DIR)
    ap.add_argument("--data-dir", default="/workspace/NOEMA/data")
    ap.add_argument("--start-sh", default=os.path.join(RUN_DIR, "start.sh"))
    ap.add_argument("--python", default="/tmp/nv/bin/python")
    ap.add_argument("--interval", type=float, default=60)
    ap.add_argument("--max-log-mb", type=float, default=50)
    ap.add_argument("--max-db-gb", type=float, default=20)
    ap.add_argument("--retention-every-s", type=float, default=1800)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    state: dict[str, float] = {}
    while True:
        res = cycle(a, state)
        print(time.strftime("%H:%M:%S"), json.dumps({k: v for k, v in res.items() if v}, default=str)[:400], flush=True)
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    raise SystemExit(main())
