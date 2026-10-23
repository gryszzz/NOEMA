"""Cold storage for append-only evidence: compressed payloads + verified zstd-Parquet compaction.

Two independent, reversible tools (paper/research data only; never ledgers of money, orders or treasury):

* ``encode_payload`` / ``decode_payload`` - large JSON text is stored as a zlib BLOB with a ``NZ1`` magic
  prefix. Readers call ``decode_payload`` on whatever the column returns: plain TEXT (older rows, small
  payloads, other databases) passes through unchanged, so old and new rows read identically.
* ``compact_table`` - rows older than a cutoff are copied into
  ``<root>/<table>/date=YYYY-MM-DD/part-<first>-<last>.parquet`` (zstd), the file is fsynced, read back and
  verified (row count, key set and a content digest over every column) and only then are exactly those rows
  deleted from SQLite. A crash between write and delete leaves the rows in SQLite and a re-run rewrites the
  same deterministic file name, so nothing is lost or double-counted (readers also dedupe on the key).
  ``read_archive`` / ``union_rows`` read archived + hot rows back transparently (hot rows win on key ties).

Parquet needs ``pyarrow`` (declared dependency). It is imported lazily: payload codecs and the hot-path
writers never import it, and compaction fails closed (no delete) when it is unavailable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
import zlib
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

MAGIC = b"NZ1"
MIN_COMPRESS_BYTES = int(os.getenv("NOEMA_PAYLOAD_COMPRESS_MIN", "1024"))
ZLIB_LEVEL = 6
PARQUET_ZSTD_LEVEL = 9


# ---- payload codec ----------------------------------------------------------------------------------
def encode_payload(text: str, *, min_bytes: int | None = None) -> str | bytes:
    """zlib-compress large text payloads; small ones stay TEXT (cheaper than the header)."""
    raw = text.encode("utf-8")
    if len(raw) < (MIN_COMPRESS_BYTES if min_bytes is None else min_bytes):
        return text
    return MAGIC + zlib.compress(raw, ZLIB_LEVEL)


def decode_payload(value: Any) -> Any:
    """Inverse of ``encode_payload``; anything that is not a NZ1 blob is returned unchanged."""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)) and bytes(value[:3]) == MAGIC:
        return zlib.decompress(bytes(value[3:])).decode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return value


def loads_payload(value: Any) -> Any:
    return json.loads(decode_payload(value))


# ---- parquet archive ----------------------------------------------------------------------------------
def default_root(db_path: str) -> str:
    """``data/cold/<db stem>`` next to the database (env ``NOEMA_COLD_DIR`` overrides the parent)."""
    p = Path(db_path).resolve()
    base = Path(os.getenv("NOEMA_COLD_DIR") or (p.parent / "cold"))
    return str(base / p.name.removesuffix(".db"))


def _pa() -> Any:
    import pyarrow as pa
    import pyarrow.parquet as pq
    return pa, pq


_ARROW = {"INTEGER": "int64", "INT": "int64", "REAL": "float64", "TEXT": "string", "BLOB": "binary"}


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    out = []
    for _, name, decl, *_ in conn.execute(f"PRAGMA table_info({table})"):
        d = (decl or "").upper()
        kind = next((v for k, v in _ARROW.items() if k in d), "string")
        out.append((name, kind))
    return out


def _coerce(kind: str, v: Any, decode: bool) -> Any:
    if v is None:
        return None
    if decode:
        v = decode_payload(v)
    if kind == "float64":
        return float(v)
    if kind == "int64":
        if isinstance(v, float) and v.is_integer():
            return int(v)
        if not isinstance(v, int) or isinstance(v, bool):
            raise TypeError(f"non-integer {v!r}")
        return v
    if kind == "string":
        if isinstance(v, (bytes, bytearray, memoryview)):
            raise TypeError("binary value in TEXT column (not a NZ1 payload)")
        return str(v)
    return bytes(v)


def _digest(rows: Iterable[Sequence[Any]]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(list(r), default=lambda b: b.hex() if isinstance(b, bytes) else str(b),
                            separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()


def _date_of(v: Any) -> str:
    if isinstance(v, (int, float)):
        return time.strftime("%Y-%m-%d", time.gmtime(float(v)))
    s = str(v or "")
    return s[:10] if len(s) >= 10 and s[4] == "-" else "unknown"


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_verified(path: Path, names: list[str], kinds: list[str], rows: list[tuple]) -> None:
    """Atomically write rows to parquet and prove the read-back equals them; raises on any mismatch."""
    pa, pq = _pa()
    schema = pa.schema([(n, getattr(pa, k)()) for n, k in zip(names, kinds, strict=True)])
    cols = list(zip(*rows, strict=True)) if rows else [[] for _ in names]
    table = pa.Table.from_arrays([pa.array(list(c), type=schema.field(i).type) for i, c in enumerate(cols)],
                                 schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd", compression_level=PARQUET_ZSTD_LEVEL)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    back = pq.read_table(tmp)
    got = list(zip(*[back.column(n).to_pylist() for n in names], strict=True))
    if back.num_rows != len(rows) or _digest(got) != _digest(rows):
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"parquet verification failed for {path.name}")
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def compact_table(conn: sqlite3.Connection, table: str, *, ts_col: str, cutoff: Any, root: str,
                  payload_cols: Sequence[str] = (), batch: int = 20000, max_rows: int | None = None,
                  pause_s: float = 0.02, should_stop: Callable[[], bool] | None = None,
                  where_extra: str = "") -> dict[str, Any]:
    """Move rows with ``ts_col < cutoff`` into verified parquet, then delete them. Returns stats."""
    out: dict[str, Any] = {"table": table, "archived": 0, "files": 0, "errors": []}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return out
    cols = _columns(conn, table)
    names = [n for n, _ in cols]
    kinds = [k for _, k in cols]
    decode = [n in payload_cols for n in names]
    extra = f" AND ({where_extra})" if where_extra else ""
    sel = (f"SELECT rowid,{','.join(names)} FROM {table} WHERE {ts_col} < ? AND rowid > ?{extra} "
           f"ORDER BY rowid LIMIT ?")
    last = -1
    while max_rows is None or out["archived"] < max_rows:
        if should_stop and should_stop():
            break
        rows = conn.execute(sel, (cutoff, last, batch)).fetchall()
        if not rows:
            break
        last = rows[-1][0]
        groups: dict[str, list[tuple]] = {}
        try:
            for r in rows:
                vals = tuple(_coerce(k, v, d) for k, v, d in zip(kinds, r[1:], decode, strict=True))
                groups.setdefault(_date_of(r[1 + names.index(ts_col)]), []).append((r[0], vals))
        except (TypeError, ValueError, zlib.error) as exc:
            out["errors"].append(f"batch ending rowid {last}: {exc}")
            continue          # leave this batch in SQLite; never delete what could not be archived
        for day, items in sorted(groups.items()):
            ids = [i for i, _ in items]
            path = Path(root) / table / f"date={day}" / f"part-{ids[0]:012d}-{ids[-1]:012d}.parquet"
            write_verified(path, names, kinds, [v for _, v in items])
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                conn.execute(f"DELETE FROM {table} WHERE rowid IN ({','.join('?' * len(chunk))})", chunk)
            conn.commit()
            out["archived"] += len(ids)
            out["files"] += 1
        if pause_s:
            time.sleep(pause_s)
    if out["archived"]:
        _update_manifest(Path(root), table)
    return out


def _update_manifest(root: Path, table: str) -> None:
    files = sorted((root / table).glob("date=*/*.parquet"))
    _, pq = _pa()
    rows = sum(pq.ParquetFile(f).metadata.num_rows for f in files)
    m = {"table": table, "files": len(files), "rows": rows,
         "bytes": sum(f.stat().st_size for f in files),
         "latest_partition": files[-1].parent.name.removeprefix("date=") if files else None,
         "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = root / table / "_manifest.json.tmp"
    tmp.write_text(json.dumps(m, indent=1))
    os.replace(tmp, root / table / "_manifest.json")


def manifest(root: str, table: str) -> dict[str, Any]:
    try:
        return json.loads((Path(root) / table / "_manifest.json").read_text())
    except (OSError, ValueError):
        return {}


def read_archive(root: str, table: str, *, columns: Sequence[str] | None = None,
                 filter_expr: Any = None) -> list[dict[str, Any]]:
    """Archived rows as dicts (``filter_expr`` is a pyarrow.dataset expression). Empty when none."""
    base = Path(root) / table
    files = sorted(str(f) for f in base.glob("date=*/*.parquet"))
    if not files:
        return []
    import pyarrow.dataset as ds
    d = ds.dataset(files, format="parquet")
    return d.to_table(columns=list(columns) if columns else None, filter=filter_expr).to_pylist()


def union_rows(conn: sqlite3.Connection, root: str, table: str, *, key: Sequence[str],
               where_sql: str = "1=1", params: Sequence[Any] = (), filter_expr: Any = None,
               columns: Sequence[str] | None = None, payload_cols: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Hot (SQLite) + cold (parquet) rows matching the same predicate, deduped on ``key`` (hot wins)."""
    names = list(columns) if columns else [n for n, _ in _columns(conn, table)]
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for r in conn.execute(f"SELECT {','.join(names)} FROM {table} WHERE {where_sql}", tuple(params)):
        row = dict(zip(names, r, strict=True))
        for c in payload_cols:
            if c in row:
                row[c] = decode_payload(row[c])
        seen.add(tuple(row[k] for k in key))
        out.append(row)
    for row in read_archive(root, table, columns=names, filter_expr=filter_expr):
        k = tuple(row[k] for k in key)
        if k not in seen:
            seen.add(k)
            out.append(row)
    return out


def recompress_payloads(conn: sqlite3.Connection, table: str, col: str, *, keep_text_if: str | None = None,
                        batch: int = 500) -> int:
    """One-time migration: re-encode existing TEXT payloads in place (lossless; verified per row)."""
    n = 0
    last = -1
    while True:
        rows = conn.execute(f"SELECT rowid,{col} FROM {table} WHERE rowid > ? AND typeof({col})='text' "
                            f"ORDER BY rowid LIMIT ?", (last, batch)).fetchall()
        if not rows:
            return n
        last = rows[-1][0]
        upd = []
        for rid, text in rows:
            if keep_text_if and keep_text_if in text:
                continue
            enc = encode_payload(text)
            if isinstance(enc, bytes):
                if decode_payload(enc) != text:
                    raise RuntimeError(f"{table}.{col} rowid {rid}: round-trip mismatch")
                upd.append((enc, rid))
        conn.executemany(f"UPDATE {table} SET {col}=? WHERE rowid=?", upd)
        conn.commit()
        n += len(upd)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compact old rows of an append-only table into zstd parquet")
    ap.add_argument("--db", required=True)
    ap.add_argument("--table", required=True)
    ap.add_argument("--ts-col", required=True)
    ap.add_argument("--older-than-hours", type=float, required=True)
    ap.add_argument("--numeric-ts", action="store_true", help="ts column is epoch seconds")
    ap.add_argument("--payload-col", action="append", default=[])
    ap.add_argument("--root")
    ap.add_argument("--batch", type=int, default=20000)
    ap.add_argument("--max-rows", type=int)
    a = ap.parse_args(argv)
    from datetime import UTC, datetime, timedelta
    cut: Any = (time.time() - a.older_than_hours * 3600 if a.numeric_ts else
                (datetime.now(UTC) - timedelta(hours=a.older_than_hours)).isoformat())
    conn = sqlite3.connect(a.db, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        res = compact_table(conn, a.table, ts_col=a.ts_col, cutoff=cut, root=a.root or default_root(a.db),
                            payload_cols=a.payload_col, batch=a.batch, max_rows=a.max_rows)
    finally:
        conn.close()
    print(json.dumps(res))
    return 0 if not res["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
