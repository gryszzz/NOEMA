"""Versioned, source-linked mechanics knowledge; empirical beliefs stay separate."""

from __future__ import annotations

import hashlib
import html.parser
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_SOURCE_BYTES = 1_000_000
MAX_CHUNK_CHARS = 2_400
MAX_RETRIEVAL_CHUNKS = 6
MAX_RETRIEVAL_CHARS = 9_000


@dataclass(frozen=True)
class SourceSeed:
    source_id: str
    url: str
    source_type: str
    trust_level: str
    domains: tuple[str, ...]
    curricula: tuple[str, ...]
    freshness_days: int = 30
    accept: str = "text/html"


# These are reading lists, not runtime agents and not evidence of economic edge.
CURRICULA: dict[str, tuple[str, tuple[str, ...]]] = {
    "shared_core": (
        "Learn core chain execution, transaction, account, and state mechanics from primary documentation before interpreting any domain-specific observation.",
        ("solana", "ethereum", "raydium"),
    ),
    "scout": (
        "Find timestamped, novel observations and explain why they merit investigation; never recommend a trade.",
        ("solana", "jupiter", "raydium"),
    ),
    "onchain": (
        "Reconstruct accounts, authority, funding, ownership, transfers, and exits from verified chain data.",
        ("solana", "raydium", "ethereum"),
    ),
    "research": (
        "Turn documented mechanics and observations into falsifiable hypotheses with explicit unknowns.",
        ("solana", "raydium", "ethereum", "amm"),
    ),
    "quant": (
        "Check baselines, calibration, chronology, leakage, sample size, and out-of-sample performance.",
        ("statistics", "prediction_markets", "web3"),
    ),
    "risk_security": (
        "Find authority, contract, custody, liquidity, resolution, and operational failure modes; report blockers.",
        ("security", "solana", "ethereum", "web3"),
    ),
    "execution": (
        "Model quote-to-fill differences, fees, impact, latency, failed transactions, and supported permissions.",
        ("execution", "solana", "jupiter", "raydium"),
    ),
    "mev": (
        "Identify adversarial ordering, competition, and execution assumptions without treating MEV as an opportunity by default.",
        ("mev", "ethereum", "solana"),
    ),
    "prediction_markets": (
        "Interpret contracts, resolution rules, probabilities, order books, fees, and calibration.",
        ("prediction_markets", "kalshi"),
    ),
    "business": (
        "Test who benefits, what deliverable they value, willingness to pay, delivery cost, gross margin, and support burden.",
        ("business", "stripe"),
    ),
}


SOURCES: tuple[SourceSeed, ...] = (
    SourceSeed(
        "solana-core",
        "https://solana.com/docs/core",
        "official_documentation",
        "primary",
        ("solana",),
        ("scout", "onchain", "research", "risk_security"),
    ),
    SourceSeed(
        "solana-transactions",
        "https://solana.com/docs/core/transactions/transaction-pipeline",
        "official_documentation",
        "primary",
        ("solana", "execution"),
        ("onchain", "execution", "risk_security"),
        accept="text/markdown",
    ),
    SourceSeed(
        "solana-rpc",
        "https://solana.com/docs/rpc",
        "official_documentation",
        "primary",
        ("solana", "rpc"),
        ("scout", "onchain", "research"),
    ),
    SourceSeed(
        "solana-fees",
        "https://solana.com/docs/core/fees",
        "official_documentation",
        "primary",
        ("solana", "execution"),
        ("execution", "quant", "risk_security"),
    ),
    SourceSeed(
        "jupiter-index",
        "https://developers.jup.ag/docs/llms.txt",
        "official_documentation_index",
        "primary",
        ("jupiter", "solana"),
        ("scout", "execution", "research"),
    ),
    SourceSeed(
        "raydium-index",
        "https://docs.raydium.io/llms.txt",
        "official_documentation_index",
        "primary",
        ("raydium", "solana"),
        ("scout", "onchain", "research", "execution", "risk_security"),
    ),
    SourceSeed(
        "raydium-launchlab",
        "https://docs.raydium.io/user-flows/launchlab-overview.md",
        "official_documentation",
        "primary",
        ("raydium", "solana"),
        ("scout", "research", "risk_security"),
    ),
    SourceSeed(
        "raydium-bonding-curves",
        "https://docs.raydium.io/algorithms/bonding-curves.md",
        "official_documentation",
        "primary",
        ("raydium", "solana", "amm"),
        ("research", "quant"),
    ),
    SourceSeed(
        "jito-docs",
        "https://docs.jito.wtf/",
        "official_documentation",
        "primary",
        ("jito", "solana", "execution", "mev"),
        ("execution", "mev", "risk_security"),
    ),
    SourceSeed(
        "ethereum-evm",
        "https://ethereum.org/developers/docs/evm/",
        "official_documentation",
        "primary",
        ("ethereum", "evm"),
        ("onchain", "research", "risk_security"),
    ),
    SourceSeed(
        "openzeppelin",
        "https://docs.openzeppelin.com/contracts/5.x",
        "official_documentation",
        "primary",
        ("ethereum", "security", "evm"),
        ("onchain", "risk_security"),
    ),
    SourceSeed(
        "kalshi-index",
        "https://docs.kalshi.com/llms.txt",
        "official_documentation_index",
        "primary",
        ("kalshi", "prediction_markets"),
        ("prediction_markets", "quant", "execution"),
    ),
    SourceSeed(
        "stripe-billing",
        "https://docs.stripe.com/billing",
        "official_documentation",
        "primary",
        ("stripe", "business"),
        ("business",),
    ),
    SourceSeed(
        "stripe-agent-skills",
        "https://docs.stripe.com/skills.md",
        "official_documentation",
        "primary",
        ("stripe", "business"),
        ("business",),
    ),
    SourceSeed(
        "uniswap-v3-paper",
        "https://app.uniswap.org/whitepaper-v3.pdf",
        "primary_research",
        "primary_research",
        ("ethereum", "amm"),
        ("research", "quant", "mev"),
    ),
    SourceSeed(
        "cfmm-research",
        "https://arxiv.org/abs/2003.10001",
        "primary_research",
        "primary_research",
        ("amm", "research"),
        ("research", "quant"),
    ),
    SourceSeed(
        "flash-boys-2",
        "https://arxiv.org/abs/1904.05234",
        "primary_research",
        "primary_research",
        ("mev", "ethereum"),
        ("mev", "risk_security"),
    ),
)


SPECIALIST_CURRICULA: dict[str, str] = {
    "noema": "shared_core",
    "kalshi-history": "prediction_markets",
    "trench-1": "scout",
    "evidence-critic": "quant",
}

ACTIVE_PROMPT_ASSIGNMENTS: tuple[dict[str, str], ...] = (
    {"specialist": "NOEMA", "curriculum": "shared_core", "role": "Persistent main agent"},
    {
        "specialist": "kalshi-history",
        "curriculum": "prediction_markets",
        "role": "Prediction-market research",
    },
    {
        "specialist": "trench-1",
        "curriculum": "scout",
        "role": "Read-only Web3 opportunity scouting",
    },
)


class _TextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "br", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _normalize_document(content: str, content_type: str) -> str:
    text = content
    if "html" in content_type.lower() or "<html" in content[:512].lower():
        parser = _TextExtractor()
        parser.feed(content)
        text = "".join(parser.parts)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line)).strip()


def _chunks(text: str) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        while len(paragraph) > MAX_CHUNK_CHARS:
            head, paragraph = paragraph[:MAX_CHUNK_CHARS], paragraph[MAX_CHUNK_CHARS:]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
        addition = ("\n\n" if current else "") + paragraph
        if len(current) + len(addition) > MAX_CHUNK_CHARS:
            chunks.append(current)
            current = paragraph
        else:
            current += addition
    if current:
        chunks.append(current)
    return chunks


def _section_chunks(text: str) -> list[tuple[str, str]]:
    """Split markdown at headings before applying the existing fixed chunk bound."""
    sections: list[tuple[str, list[str]]] = []
    headings: list[tuple[int, str]] = []
    body: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            if body or headings:
                sections.append((" > ".join(title for _, title in headings), body))
            level, title = len(match.group(1)), match.group(2).strip()
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title))
            body = []
        else:
            body.append(line)
    if body or headings:
        sections.append((" > ".join(title for _, title in headings), body))
    if not sections:
        sections.append(("document", text.splitlines()))
    result: list[tuple[str, str]] = []
    for section, lines in sections:
        content = "\n".join(lines).strip()
        if not content:
            continue
        for chunk in _chunks(content):
            result.append((section or "document", chunk))
    return result


def _words(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9]{3,}", text.lower())
        if word not in {"the", "and", "for", "with", "from", "that", "this", "are", "how"}
    }


class KnowledgeStore:
    """Source snapshots and empirical belief revisions, isolated from authority."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_sources (
                source_id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, source_type TEXT NOT NULL,
                trust_level TEXT NOT NULL, domains_json TEXT NOT NULL, curricula_json TEXT NOT NULL,
                freshness_days INTEGER NOT NULL, registered_at TEXT NOT NULL,
                last_checked_at TEXT, last_fetched_at TEXT, latest_version_id TEXT,
                last_status TEXT NOT NULL DEFAULT 'never_fetched');
            CREATE TABLE IF NOT EXISTS knowledge_source_checks (
                check_id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, checked_at TEXT NOT NULL,
                status TEXT NOT NULL, version_id INTEGER, detail_code TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(source_id) REFERENCES knowledge_sources(source_id));
            CREATE TABLE IF NOT EXISTS knowledge_versions (
                version_id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, source_url TEXT NOT NULL,
                version TEXT NOT NULL,
                fetched_at TEXT NOT NULL, content_hash TEXT NOT NULL, content_type TEXT NOT NULL,
                normalized_text TEXT NOT NULL, UNIQUE(source_id,content_hash),
                FOREIGN KEY(source_id) REFERENCES knowledge_sources(source_id));
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id TEXT PRIMARY KEY, version_id INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                content_hash TEXT NOT NULL, text TEXT NOT NULL,
                section_path TEXT NOT NULL DEFAULT '', UNIQUE(version_id,ordinal),
                FOREIGN KEY(version_id) REFERENCES knowledge_versions(version_id));
            CREATE TABLE IF NOT EXISTS knowledge_retrievals (
                retrieval_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, specialist TEXT NOT NULL,
                curriculum TEXT NOT NULL, mission_hash TEXT NOT NULL, chunk_ids_json TEXT NOT NULL,
                citations_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_belief_revisions (
                belief_id TEXT NOT NULL, revision INTEGER NOT NULL, claim TEXT NOT NULL,
                domain TEXT NOT NULL, supporting_evidence_json TEXT NOT NULL,
                contradicting_evidence_json TEXT NOT NULL, confidence REAL,
                applicable_regime TEXT, first_observed TEXT NOT NULL, last_verified TEXT NOT NULL,
                source_provenance_json TEXT NOT NULL, sample_size INTEGER NOT NULL,
                validation_state TEXT NOT NULL, expires_at TEXT,
                PRIMARY KEY(belief_id,revision));
            CREATE TRIGGER IF NOT EXISTS knowledge_versions_no_update
              BEFORE UPDATE ON knowledge_versions BEGIN SELECT RAISE(ABORT,'immutable knowledge versions'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_versions_no_delete
              BEFORE DELETE ON knowledge_versions BEGIN SELECT RAISE(ABORT,'immutable knowledge versions'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_chunks_no_update
              BEFORE UPDATE ON knowledge_chunks BEGIN SELECT RAISE(ABORT,'immutable knowledge chunks'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_chunks_no_delete
              BEFORE DELETE ON knowledge_chunks BEGIN SELECT RAISE(ABORT,'immutable knowledge chunks'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_beliefs_no_update
              BEFORE UPDATE ON knowledge_belief_revisions BEGIN SELECT RAISE(ABORT,'append belief revisions'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_beliefs_no_delete
              BEFORE DELETE ON knowledge_belief_revisions BEGIN SELECT RAISE(ABORT,'append belief revisions'); END;
        """)
        source_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(knowledge_sources)")
        }
        if "last_fetched_at" not in source_columns:
            self.conn.execute("ALTER TABLE knowledge_sources ADD COLUMN last_fetched_at TEXT")
        version_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(knowledge_versions)")
        }
        if "source_url" not in version_columns:
            self.conn.execute("ALTER TABLE knowledge_versions ADD COLUMN source_url TEXT")
        chunk_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(knowledge_chunks)")
        }
        if "section_path" not in chunk_columns:
            self.conn.execute(
                "ALTER TABLE knowledge_chunks ADD COLUMN section_path TEXT NOT NULL DEFAULT ''"
            )
        belief_columns = {
            row[1]: row
            for row in self.conn.execute("PRAGMA table_info(knowledge_belief_revisions)")
        }
        if belief_columns.get("confidence", (None,) * 4)[3]:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "ALTER TABLE knowledge_belief_revisions RENAME TO knowledge_belief_revisions_old"
            )
            self.conn.execute("""CREATE TABLE knowledge_belief_revisions (
                belief_id TEXT NOT NULL, revision INTEGER NOT NULL, claim TEXT NOT NULL,
                domain TEXT NOT NULL, supporting_evidence_json TEXT NOT NULL,
                contradicting_evidence_json TEXT NOT NULL, confidence REAL,
                applicable_regime TEXT, first_observed TEXT NOT NULL, last_verified TEXT NOT NULL,
                source_provenance_json TEXT NOT NULL, sample_size INTEGER NOT NULL,
                validation_state TEXT NOT NULL, expires_at TEXT,
                PRIMARY KEY(belief_id,revision))""")
            self.conn.execute(
                "INSERT INTO knowledge_belief_revisions SELECT * FROM knowledge_belief_revisions_old"
            )
            self.conn.execute("DROP TABLE knowledge_belief_revisions_old")
            self.conn.commit()
        self.conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS knowledge_versions_no_update
              BEFORE UPDATE ON knowledge_versions BEGIN SELECT RAISE(ABORT,'immutable knowledge versions'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_versions_no_delete
              BEFORE DELETE ON knowledge_versions BEGIN SELECT RAISE(ABORT,'immutable knowledge versions'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_chunks_no_update
              BEFORE UPDATE ON knowledge_chunks BEGIN SELECT RAISE(ABORT,'immutable knowledge chunks'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_chunks_no_delete
              BEFORE DELETE ON knowledge_chunks BEGIN SELECT RAISE(ABORT,'immutable knowledge chunks'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_beliefs_no_update
              BEFORE UPDATE ON knowledge_belief_revisions BEGIN SELECT RAISE(ABORT,'append belief revisions'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_beliefs_no_delete
              BEFORE DELETE ON knowledge_belief_revisions BEGIN SELECT RAISE(ABORT,'append belief revisions'); END;
        """)
        self.conn.commit()
        self.seed_registry()

    def seed_registry(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self.conn:
            for source in SOURCES:
                self.conn.execute(
                    "INSERT INTO knowledge_sources(source_id,url,source_type,trust_level,"
                    "domains_json,curricula_json,freshness_days,registered_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source_id) DO UPDATE SET url=excluded.url,source_type=excluded.source_type,"
                    "trust_level=excluded.trust_level,domains_json=excluded.domains_json,"
                    "curricula_json=excluded.curricula_json,freshness_days=excluded.freshness_days",
                    (
                        source.source_id,
                        source.url,
                        source.source_type,
                        source.trust_level,
                        json.dumps(source.domains),
                        json.dumps(source.curricula),
                        source.freshness_days,
                        now,
                    ),
                )

    def sources(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        result = []
        for row in self.conn.execute("SELECT * FROM knowledge_sources ORDER BY source_id"):
            check = self.conn.execute(
                "SELECT status,detail_code FROM knowledge_source_checks "
                "WHERE source_id=? ORDER BY check_id DESC LIMIT 1",
                (row["source_id"],),
            ).fetchone()
            checked = (
                datetime.fromisoformat(row["last_checked_at"]) if row["last_checked_at"] else None
            )
            fetched = (
                datetime.fromisoformat(row["last_fetched_at"]) if row["last_fetched_at"] else None
            )
            freshness = (
                "never_fetched"
                if fetched is None
                else ("stale" if now - fetched > timedelta(days=row["freshness_days"]) else "fresh")
            )
            result.append(
                {
                    "source_id": row["source_id"],
                    "url": row["url"],
                    "source_type": row["source_type"],
                    "trust_level": row["trust_level"],
                    "domains": json.loads(row["domains_json"]),
                    "curricula": json.loads(row["curricula_json"]),
                    "freshness_days": row["freshness_days"],
                    "last_checked_at": checked.isoformat() if checked else None,
                    "last_fetched_at": fetched.isoformat() if fetched else None,
                    "latest_version_id": row["latest_version_id"],
                    "status": row["last_status"],
                    "last_check_status": check["status"] if check else None,
                    "last_detail_code": check["detail_code"] if check else None,
                    "freshness": freshness,
                }
            )
        return result

    def refresh_source(self, source_id: str, *, timeout: float = 15.0) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM knowledge_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if row is None:
            raise KeyError(source_id)
        url = str(row["url"])
        seed = next((item for item in SOURCES if item.source_id == source_id), None)
        if seed is None or seed.url != url:
            raise ValueError("source is not in the trusted registry")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("registered source URL is not safe")
        checked_at = datetime.now(UTC).isoformat()
        status, detail, version_id, fetched_at = "failed", "provider_error", None, None
        try:
            with (
                    httpx.Client(
                        timeout=timeout,
                        follow_redirects=False,
                    headers={"User-Agent": "NOEMA-Knowledge/1.0", "Accept": seed.accept},
                ) as client,
                client.stream("GET", url) as response,
            ):
                if response.status_code != 200:
                    detail = f"http_{response.status_code}"
                else:
                    chunks: list[bytes] = []
                    size = 0
                    for part in response.iter_bytes():
                        size += len(part)
                        if size > MAX_SOURCE_BYTES:
                            detail = "source_size_limit"
                            break
                        chunks.append(part)
                    else:
                        raw = b"".join(chunks)
                        content_type = response.headers.get("content-type", "text/plain")[:120]
                        if (
                            "pdf" in content_type.lower()
                            or b"\x00" in raw[:1024]
                            or not content_type.lower().startswith(
                                ("text/", "application/json", "application/xml")
                            )
                        ):
                            detail = "unsupported_content_type"
                        else:
                            text = _normalize_document(raw.decode("utf-8-sig"), content_type)
                            if not text:
                                detail = "empty_source"
                            else:
                                digest = hashlib.sha256(raw).hexdigest()
                                fetched_at = datetime.now(UTC).isoformat()
                                with self.conn:
                                    cur = self.conn.execute(
                                        "INSERT OR IGNORE INTO knowledge_versions(source_id,source_url,version,fetched_at,content_hash,content_type,normalized_text) VALUES(?,?,?,?,?,?,?)",
                                        (
                                            source_id,
                                            url,
                                            digest[:16],
                                            fetched_at,
                                            digest,
                                            content_type,
                                            text,
                                        ),
                                    )
                                    existing = self.conn.execute(
                                        "SELECT version_id FROM knowledge_versions WHERE source_id=? AND content_hash=?",
                                        (source_id, digest),
                                    ).fetchone()
                                    version_id = int(existing[0])
                                    if cur.rowcount:
                                        for ordinal, (section_path, piece) in enumerate(
                                            _section_chunks(text)
                                        ):
                                            chunk_id = hashlib.sha256(
                                                f"{source_id}:{digest}:{section_path}:{ordinal}".encode()
                                            ).hexdigest()[:32]
                                            self.conn.execute(
                                                "INSERT INTO knowledge_chunks(chunk_id,version_id,ordinal,content_hash,text,section_path) VALUES(?,?,?,?,?,?)",
                                                (
                                                    chunk_id,
                                                    version_id,
                                                    ordinal,
                                                    hashlib.sha256(piece.encode()).hexdigest(),
                                                    piece,
                                                    section_path,
                                                ),
                                            )
                                status, detail = "fetched", ""
        except (httpx.HTTPError, UnicodeDecodeError, OSError, sqlite3.Error):
            status, detail = "failed", "provider_or_storage_error"
        with self.conn:
            self.conn.execute(
                "INSERT INTO knowledge_source_checks(source_id,checked_at,status,version_id,detail_code) VALUES(?,?,?,?,?)",
                (source_id, checked_at, status, version_id, detail),
            )
            self.conn.execute(
                "UPDATE knowledge_sources SET last_checked_at=?,last_fetched_at=COALESCE(?,last_fetched_at),"
                "latest_version_id=COALESCE(?,latest_version_id),last_status=? WHERE source_id=?",
                (checked_at, fetched_at, version_id, status, source_id),
            )
        return {
            "source_id": source_id,
            "status": status,
            "detail_code": detail,
            "version_id": version_id,
            "checked_at": checked_at,
        }

    def refresh_due(self, *, limit: int = 5) -> list[dict[str, Any]]:
        if not 1 <= limit <= 20:
            raise ValueError("refresh limit must be in [1,20]")
        due = [item for item in self.sources() if item["freshness"] != "fresh"][:limit]
        return [self.refresh_source(item["source_id"]) for item in due]

    def retrieve(
        self, *, specialist: str, mission: str, domain: str | None = None, limit: int = 3
    ) -> list[dict[str, Any]]:
        if not mission.strip() or not 1 <= limit <= MAX_RETRIEVAL_CHUNKS or len(mission) > 1200:
            raise ValueError("retrieval bounds invalid")
        curriculum = SPECIALIST_CURRICULA.get(specialist, "research")
        curriculum_domains = set(CURRICULA.get(curriculum, CURRICULA["research"])[1])
        shared_domains = set(CURRICULA["shared_core"][1])
        query_domains = (
            shared_domains | curriculum_domains | ({domain.lower()} if domain else set())
        )
        query_words = _words(mission)
        rows = self.conn.execute(
            "SELECT c.chunk_id,c.text,c.section_path,v.version,v.fetched_at AS version_created_at,v.source_id,"
            "COALESCE(v.source_url,s.url) AS url,s.source_type,s.trust_level,s.domains_json,s.curricula_json,s.freshness_days,"
            "s.last_fetched_at AS source_fetched_at "
            "FROM knowledge_chunks c JOIN knowledge_versions v ON v.version_id=c.version_id "
            "JOIN knowledge_sources s ON s.source_id=v.source_id ORDER BY v.fetched_at DESC,c.ordinal"
        ).fetchall()
        candidates = []
        now = datetime.now(UTC)
        for row in rows:
            source_domains = set(json.loads(row["domains_json"]))
            source_curricula = set(json.loads(row["curricula_json"]))
            if not (query_domains & source_domains or curriculum in source_curricula):
                continue
            text = str(row["text"])
            overlap = len(query_words & _words(text))
            if query_words and overlap == 0:
                continue
            fetched = datetime.fromisoformat(row["source_fetched_at"] or row["version_created_at"])
            fresh = now - fetched <= timedelta(days=int(row["freshness_days"]))
            score = overlap + (2 if curriculum in source_curricula else 0) + (1 if fresh else -2)
            candidates.append((score, row, fresh))
        candidates.sort(
            key=lambda item: (
                item[0],
                item[1]["source_fetched_at"] or item[1]["version_created_at"],
            ),
            reverse=True,
        )
        result = []
        used = 0
        selected_sources: set[str] = set()
        for score, row, fresh in candidates:
            if row["source_id"] in selected_sources:
                continue
            text = str(row["text"])
            if used + len(text) > MAX_RETRIEVAL_CHARS:
                continue
            result.append(
                {
                    "chunk_id": row["chunk_id"],
                    "section_identity": row["section_path"] or "document",
                    "source_id": row["source_id"],
                    "url": row["url"],
                    "source_type": row["source_type"],
                    "trust_level": row["trust_level"],
                    "version": row["version"],
                    "fetched_at": row["source_fetched_at"] or row["version_created_at"],
                    "version_created_at": row["version_created_at"],
                    "freshness": "fresh" if fresh else "stale",
                    "relevance_score": score,
                    "text": text,
                }
            )
            used += len(text)
            selected_sources.add(str(row["source_id"]))
            if len(result) == limit:
                break
        if result:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO knowledge_retrievals(created_at,specialist,curriculum,mission_hash,chunk_ids_json,citations_json) VALUES(?,?,?,?,?,?)",
                    (
                        now.isoformat(),
                        specialist,
                        curriculum,
                        hashlib.sha256(mission.encode()).hexdigest(),
                        json.dumps([item["chunk_id"] for item in result]),
                        json.dumps(
                            [
                                {
                                    k: item[k]
                                    for k in (
                                        "chunk_id",
                                        "section_identity",
                                        "source_id",
                                        "url",
                                        "version",
                                        "fetched_at",
                                        "freshness",
                                    )
                                }
                                for item in result
                            ],
                            sort_keys=True,
                        ),
                    ),
                )
        return result

    def append_belief_revision(
        self,
        *,
        belief_id: str,
        claim: str,
        domain: str,
        supporting_evidence: list[dict[str, str]],
        contradicting_evidence: list[dict[str, str]],
        confidence: float | None,
        applicable_regime: str | None,
        first_observed: str,
        last_verified: str,
        source_provenance: list[dict[str, str]],
        sample_size: int,
        validation_state: str,
        expires_at: str | None = None,
    ) -> int:
        if (
            not belief_id.strip()
            or len(belief_id) > 200
            or not claim.strip()
            or len(claim) > 2000
            or not domain.strip()
            or len(domain) > 120
            or (applicable_regime is not None and len(applicable_regime) > 500)
            or (
                confidence is not None
                and (
                    type(confidence) not in (int, float)
                    or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1
                )
            )
            or type(sample_size) is not int
            or sample_size < 0
            or validation_state
            not in {
                "documented_fact",
                "hypothesis",
                "empirically_supported",
                "contradicted",
                "unvalidated",
            }
        ):
            raise ValueError("invalid belief revision")
        for references in (supporting_evidence, contradicting_evidence, source_provenance):
            if not isinstance(references, list) or len(references) > 100:
                raise ValueError("belief evidence list exceeds its bound")
            for reference in references:
                if (
                    not isinstance(reference, dict)
                    or len(reference) > 12
                    or any(
                        not isinstance(key, str)
                        or not isinstance(value, str)
                        or len(key) > 80
                        or len(value) > 2000
                        for key, value in reference.items()
                    )
                ):
                    raise ValueError("belief evidence reference is invalid")
        if validation_state == "documented_fact" and not source_provenance:
            raise ValueError("documented facts require source provenance")
        if validation_state == "empirically_supported" and (
            sample_size < 1 or not supporting_evidence
        ):
            raise ValueError("empirical beliefs require measured support and sample size")
        if validation_state == "contradicted" and not contradicting_evidence:
            raise ValueError("contradicted beliefs require contradicting evidence")
        for stamp in (first_observed, last_verified, expires_at):
            if stamp is not None and datetime.fromisoformat(stamp).tzinfo is None:
                raise ValueError("belief timestamps must be timezone-aware")
        prev = self.conn.execute(
            "SELECT MAX(revision) FROM knowledge_belief_revisions WHERE belief_id=?", (belief_id,)
        ).fetchone()[0]
        revision = int(prev or 0) + 1
        with self.conn:
            self.conn.execute(
                "INSERT INTO knowledge_belief_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    belief_id,
                    revision,
                    claim,
                    domain,
                    json.dumps(supporting_evidence, sort_keys=True),
                    json.dumps(contradicting_evidence, sort_keys=True),
                    confidence,
                    applicable_regime,
                    first_observed,
                    last_verified,
                    json.dumps(source_provenance, sort_keys=True),
                    sample_size,
                    validation_state,
                    expires_at,
                ),
            )
        return revision

    def record_evaluated_result(
        self,
        *,
        trial_id: str,
        family: str,
        feature_set_version: str,
        trial_created_at: str,
        kind: str,
        evidence_hash: str,
        result: dict[str, Any],
    ) -> int | None:
        """Translate only an independently accepted measured result into a belief revision."""
        critic = result.get("critic_review")
        if (
            not isinstance(critic, dict)
            or critic.get("result_accepted") is not True
            or result.get("live_eligible") is not False
            or not re.fullmatch(r"[0-9a-f]{64}", evidence_hash)
        ):
            return None
        sample_size = 0
        state = "unvalidated"
        confidence = None
        supports: list[dict[str, str]] = []
        contradicts: list[dict[str, str]] = []
        claim = "The bounded research result has not established an empirically supported claim."
        metrics: dict[str, str] = {"result_status": str(result.get("status", "unknown"))}
        if kind == "trench_survival_logistic":
            from .trench_survival_model import MIN_TEST_LABELS, MIN_TRAIN_LABELS

            training = result.get("training_labels")
            walk_forward = result.get("walk_forward_tests")
            model = result.get("model_brier")
            baseline = result.get("baseline_brier")
            sample_size = walk_forward if type(walk_forward) is int and walk_forward > 0 else 0
            metrics.update(
                {
                    "model_version": str(result.get("model_version", "unknown")),
                    "training_labels": str(training),
                    "walk_forward_labels": str(walk_forward),
                    "model_brier": str(model),
                    "baseline_brier": str(baseline),
                    "audit_status": str(result.get("status", "unknown")),
                }
            )
            if (
                type(training) is int
                and training >= MIN_TRAIN_LABELS
                and type(walk_forward) is int
                and walk_forward >= MIN_TEST_LABELS
                and type(model) in (float, int)
                and type(baseline) in (float, int)
                and math.isfinite(float(model))
                and math.isfinite(float(baseline))
            ):
                supports.append(
                    {"evidence_id": evidence_hash, "type": "frozen_walk_forward_dataset"}
                )
                sample_size = walk_forward
                if model < baseline:
                    state = "empirically_supported"
                    claim = "The predeclared walk-forward survival model improved Brier score over its baseline on this evaluated cohort; this is forecasting evidence, not proof of net profit or execution edge."
                else:
                    state = "contradicted"
                    contradicts.append(supports.pop())
                    claim = "The predeclared walk-forward survival model did not improve Brier score over its baseline on this evaluated cohort."
            else:
                state = "hypothesis"
                claim = "The Web3 survival hypothesis lacks the complete chronological training and walk-forward sample needed for evaluation."
        elif kind == "market_data_quality":
            observed = result.get("observations")
            valid = result.get("valid_markets")
            if (
                type(observed) is int
                and observed > 0
                and type(valid) is int
                and 0 <= valid <= observed
            ):
                sample_size = observed
                state = "empirically_supported"
                supports.append(
                    {"evidence_id": evidence_hash, "type": "frozen_market_data_quality_snapshot"}
                )
                claim = f"The bounded market-data audit observed {valid} valid market records among {observed} reviewed records; this measures that snapshot only."
                metrics.update({"observations": str(observed), "valid_markets": str(valid)})
        elif kind == "commercial_opportunity_scan":
            observed = result.get("metadata_records_reviewed")
            buyers = result.get("verified_buyer_count")
            payouts = result.get("verified_payout_count")
            if type(observed) is int and observed > 0 and buyers == 0 and payouts == 0:
                sample_size = observed
                state = "empirically_supported"
                supports.append(
                    {"evidence_id": evidence_hash, "type": "frozen_public_listing_metadata"}
                )
                claim = f"This bounded public-listing scan verified no buyer or payout among {observed} reviewed metadata records; it does not establish that demand is absent."
                metrics.update(
                    {
                        "metadata_records_reviewed": str(observed),
                        "verified_buyers": "0",
                        "verified_payouts": "0",
                    }
                )
        else:
            return None

        prior_revisions = self.conn.execute(
            "SELECT revision,source_provenance_json FROM knowledge_belief_revisions "
            "WHERE belief_id=? ORDER BY revision DESC",
            (f"trial:{trial_id}",),
        ).fetchall()
        for prior in prior_revisions:
            try:
                previous = json.loads(prior["source_provenance_json"])
                if any(item.get("evidence_hash") == evidence_hash for item in previous):
                    return int(prior["revision"])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        provenance = [
            {
                "type": "independent_deterministic_critic",
                "critic": str(critic.get("critic", "unknown")),
                "evidence_hash": evidence_hash,
                "experiment_kind": kind,
                **metrics,
            }
        ]
        return self.append_belief_revision(
            belief_id=f"trial:{trial_id}",
            claim=claim,
            domain=family,
            supporting_evidence=supports,
            contradicting_evidence=contradicts,
            confidence=confidence,
            applicable_regime=feature_set_version,
            first_observed=trial_created_at,
            last_verified=datetime.now(UTC).isoformat(),
            source_provenance=provenance,
            sample_size=sample_size,
            validation_state=state,
        )

    def beliefs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT b.* FROM knowledge_belief_revisions b JOIN (SELECT belief_id,MAX(revision) revision FROM knowledge_belief_revisions GROUP BY belief_id) latest USING(belief_id,revision) ORDER BY last_verified DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [
            {
                **dict(row),
                "supporting_evidence": json.loads(row["supporting_evidence_json"]),
                "contradicting_evidence": json.loads(row["contradicting_evidence_json"]),
                "source_provenance": json.loads(row["source_provenance_json"]),
            }
            for row in rows
        ]

    def overview(self) -> dict[str, Any]:
        sources = self.sources()
        beliefs = self.beliefs(limit=25)
        return {
            "status": "recorded",
            "source_count": len(sources),
            "fetched_source_count": sum(item["latest_version_id"] is not None for item in sources),
            "stale_source_count": sum(item["freshness"] == "stale" for item in sources),
            "failed_source_count": sum(item["status"] == "failed" for item in sources),
            "never_fetched_source_count": sum(
                item["freshness"] == "never_fetched" for item in sources
            ),
            "source_status": [
                {
                    k: item[k]
                    for k in (
                        "source_id",
                        "url",
                        "source_type",
                        "trust_level",
                        "domains",
                        "last_checked_at",
                        "last_fetched_at",
                        "status",
                        "last_check_status",
                        "last_detail_code",
                        "freshness",
                    )
                }
                for item in sources
            ],
            "belief_count": len(beliefs),
            "beliefs": beliefs,
            "curriculum_assignments": list(ACTIVE_PROMPT_ASSIGNMENTS),
        }

    def close(self) -> None:
        self.conn.close()


def build_knowledge_overview(path: str) -> dict[str, Any]:
    """Read-only dashboard projection; never seeds or migrates the runtime database."""
    db = Path(path)
    if not db.is_file():
        return {
            "status": "unavailable",
            "database_present": False,
            "source_count": 0,
            "fetched_source_count": 0,
            "stale_source_count": 0,
            "failed_source_count": 0,
            "never_fetched_source_count": 0,
            "source_status": [],
            "belief_count": 0,
            "beliefs": [],
            "recent_retrievals": [],
            "curriculum_assignments": list(ACTIVE_PROMPT_ASSIGNMENTS),
        }
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "knowledge_sources" not in tables:
            return {
                "status": "not_initialized",
                "database_present": True,
                "source_count": 0,
                "fetched_source_count": 0,
                "stale_source_count": 0,
                "failed_source_count": 0,
                "never_fetched_source_count": 0,
                "source_status": [],
                "belief_count": 0,
                "beliefs": [],
                "recent_retrievals": [],
                "curriculum_assignments": list(ACTIVE_PROMPT_ASSIGNMENTS),
            }
        now = datetime.now(UTC)
        has_source_checks = "knowledge_source_checks" in tables
        sources = []
        for row in conn.execute("SELECT * FROM knowledge_sources ORDER BY source_id"):
            check = (conn.execute(
                "SELECT status,detail_code FROM knowledge_source_checks "
                "WHERE source_id=? ORDER BY check_id DESC LIMIT 1", (row["source_id"],)
            ).fetchone() if has_source_checks else None)
            checked = (
                datetime.fromisoformat(row["last_checked_at"]) if row["last_checked_at"] else None
            )
            fetched = (
                datetime.fromisoformat(row["last_fetched_at"]) if row["last_fetched_at"] else None
            )
            freshness = (
                "never_fetched"
                if fetched is None
                else ("stale" if now - fetched > timedelta(days=row["freshness_days"]) else "fresh")
            )
            sources.append(
                {
                    "source_id": row["source_id"],
                    "url": row["url"],
                    "source_type": row["source_type"],
                    "trust_level": row["trust_level"],
                    "domains": json.loads(row["domains_json"]),
                    "last_checked_at": checked.isoformat() if checked else None,
                    "last_fetched_at": fetched.isoformat() if fetched else None,
                    "status": row["last_status"],
                    "last_check_status": check["status"] if check else None,
                    "last_detail_code": check["detail_code"] if check else None,
                    "freshness": freshness,
                    "version_id": row["latest_version_id"],
                }
            )
        beliefs = []
        if "knowledge_belief_revisions" in tables:
            rows = conn.execute(
                "SELECT b.* FROM knowledge_belief_revisions b JOIN (SELECT belief_id,MAX(revision) revision FROM knowledge_belief_revisions GROUP BY belief_id) latest USING(belief_id,revision) ORDER BY last_verified DESC LIMIT 25"
            ).fetchall()
            beliefs = [
                {
                    **dict(row),
                    "supporting_evidence": json.loads(row["supporting_evidence_json"]),
                    "contradicting_evidence": json.loads(row["contradicting_evidence_json"]),
                    "source_provenance": json.loads(row["source_provenance_json"]),
                }
                for row in rows
            ]
        retrievals = []
        if "knowledge_retrievals" in tables:
            retrievals = [
                {
                    **dict(row),
                    "chunk_ids": json.loads(row["chunk_ids_json"]),
                    "citations": json.loads(row["citations_json"]),
                }
                for row in conn.execute(
                    "SELECT * FROM knowledge_retrievals ORDER BY retrieval_id DESC LIMIT 10"
                )
            ]
        return {
            "status": "recorded",
            "database_present": True,
            "source_count": len(sources),
            "fetched_source_count": sum(item["version_id"] is not None for item in sources),
            "stale_source_count": sum(item["freshness"] == "stale" for item in sources),
            "failed_source_count": sum(item["status"] == "failed" for item in sources),
            "never_fetched_source_count": sum(
                item["freshness"] == "never_fetched" for item in sources
            ),
            "source_status": sources,
            "belief_count": len(beliefs),
            "beliefs": beliefs,
            "recent_retrievals": retrievals,
            "curriculum_assignments": list(ACTIVE_PROMPT_ASSIGNMENTS),
            "authority_note": "Knowledge retrieval grants no tool, wallet, payment, or execution authority.",
        }
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        return {
            "status": "degraded",
            "database_present": True,
            "source_count": 0,
            "fetched_source_count": 0,
            "stale_source_count": 0,
            "failed_source_count": 0,
            "never_fetched_source_count": 0,
            "source_status": [],
            "belief_count": 0,
            "beliefs": [],
            "recent_retrievals": [],
            "curriculum_assignments": list(ACTIVE_PROMPT_ASSIGNMENTS),
        }
    finally:
        conn.close()
