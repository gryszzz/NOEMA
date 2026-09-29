"""Conservative canonical identities for overlapping prediction contracts.

Identity matching establishes that two venue contracts refer to the same
event/outcome. It does not establish that their resolution rules or payoffs are
equivalent; those remain explicit, separately evidenced properties.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

_MLB_TEAMS: dict[str, tuple[str, ...]] = {
    "ARI": ("arizona diamondbacks", "diamondbacks", "dbacks", "ari"),
    "ATL": ("atlanta braves", "braves", "atl"),
    "BAL": ("baltimore orioles", "orioles", "bal"),
    "BOS": ("boston red sox", "red sox", "bos"),
    "CHC": ("chicago cubs", "cubs", "chc"),
    "CWS": ("chicago white sox", "white sox", "chw", "cws"),
    "CIN": ("cincinnati reds", "reds", "cin"),
    "CLE": ("cleveland guardians", "guardians", "cle"),
    "COL": ("colorado rockies", "rockies", "col"),
    "DET": ("detroit tigers", "tigers", "det"),
    "HOU": ("houston astros", "astros", "hou"),
    "KC": ("kansas city royals", "royals", "kcr", "kc"),
    "LAA": ("los angeles angels", "angels", "laa"),
    "LAD": ("los angeles dodgers", "dodgers", "lad"),
    "MIA": ("miami marlins", "marlins", "mia"),
    "MIL": ("milwaukee brewers", "brewers", "mil"),
    "MIN": ("minnesota twins", "twins", "min"),
    "NYM": ("new york mets", "mets", "nym"),
    "NYY": ("new york yankees", "yankees", "nyy"),
    "OAK": ("oakland athletics", "oakland a's", "athletics", "oak"),
    "PHI": ("philadelphia phillies", "phillies", "phi"),
    "PIT": ("pittsburgh pirates", "pirates", "pit"),
    "SD": ("san diego padres", "padres", "sdp", "sd"),
    "SF": ("san francisco giants", "giants", "sfg", "sf"),
    "SEA": ("seattle mariners", "mariners", "sea"),
    "STL": ("st louis cardinals", "saint louis cardinals", "cardinals", "stl"),
    "TB": ("tampa bay rays", "rays", "tbr", "tb"),
    "TEX": ("texas rangers", "rangers", "tex"),
    "TOR": ("toronto blue jays", "blue jays", "tor"),
    "WSH": ("washington nationals", "nationals", "nats", "wsn", "wsh"),
}


def _words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _resolve_team(value: str) -> str | None:
    normalized = _words(value)
    if not normalized:
        return None
    tokens = set(normalized.split())
    matches: set[str] = set()
    for team_id, aliases in _MLB_TEAMS.items():
        for alias in aliases:
            alias_words = _words(alias)
            if normalized == alias_words or (
                len(alias_words) > 3 and f" {alias_words} " in f" {normalized} "
            ) or len(alias_words) <= 3 and alias_words in tokens:
                matches.add(team_id)
    return next(iter(matches)) if len(matches) == 1 else None


def _rules_hash(rules: str | None) -> str | None:
    if not rules or not rules.strip():
        return None
    normalized = " ".join(rules.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CanonicalContractIdentity:
    venue: str
    contract_id: str
    canonical_event_id: str | None
    canonical_proposition_id: str | None
    sport: str | None
    season: int | None
    event_type: str | None
    outcome_id: str | None
    settlement_rule_sha256: str | None
    identity_status: str
    # Generic ontology fields. They are optional for older persisted records.
    topic_id: str | None = None
    proposition_type: str | None = None
    canonical_outcome_id: str | None = None
    source_rule_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def identify_mlb_world_series_champion(
    *, venue: str, contract_id: str, season: int, title: str,
    outcome_text: str, resolution_rules: str | None = None,
) -> CanonicalContractIdentity:
    """Map a known MLB championship contract into a stable event/outcome ID.

    This adapter is deliberately narrow. An unknown or ambiguous club remains
    unidentified instead of receiving a fuzzy match.
    """
    event_id = f"sports:mlb:{season}:world-series-champion"
    outcome_id = _resolve_team(f"{outcome_text} {title}")
    if outcome_id is None:
        outcome_id = _resolve_team(outcome_text)
    status = "identified" if outcome_id else "unknown_outcome"
    proposition_id = f"{event_id}:winner:{outcome_id}" if outcome_id else None
    return CanonicalContractIdentity(
        venue=venue,
        contract_id=contract_id,
        canonical_event_id=event_id,
        canonical_proposition_id=proposition_id,
        sport="MLB",
        season=season,
        event_type="world_series_champion",
        outcome_id=outcome_id,
        settlement_rule_sha256=_rules_hash(resolution_rules),
        identity_status=status,
        topic_id="sports:baseball",
        proposition_type="winner",
        canonical_outcome_id=outcome_id,
    )


def identify_structured_contract(
    *, venue: str, contract_id: str, topic_id: str | None,
    canonical_event_id: str | None, proposition_type: str | None,
    canonical_outcome_id: str | None, resolution_rules: str | None = None,
    source_rule_version: str | None = None,
) -> CanonicalContractIdentity:
    """Build identity only from explicit, deterministic source mappings.

    This is the extension point for venue/family resolvers. Titles and fuzzy
    similarity are deliberately not accepted as identity evidence.
    """
    event_id = canonical_event_id.strip() if canonical_event_id else None
    topic = topic_id.strip() if topic_id else None
    proposition = proposition_type.strip() if proposition_type else None
    outcome = canonical_outcome_id.strip() if canonical_outcome_id else None
    proposition_id = (
        f"{event_id}:{proposition}:{outcome}"
        if event_id and proposition and outcome else None
    )
    return CanonicalContractIdentity(
        venue=venue, contract_id=contract_id,
        canonical_event_id=event_id,
        canonical_proposition_id=proposition_id,
        sport=None, season=None, event_type=proposition,
        outcome_id=outcome, settlement_rule_sha256=_rules_hash(resolution_rules),
        identity_status=("identified" if proposition_id else "unresolved"),
        topic_id=topic, proposition_type=proposition,
        canonical_outcome_id=outcome, source_rule_version=source_rule_version,
    )


Resolver = Callable[..., CanonicalContractIdentity]
_RESOLVERS: dict[tuple[str, str], Resolver] = {
    ("sports:baseball", "world_series_champion"): identify_mlb_world_series_champion,
}


def register_resolver(topic_id: str, proposition_family: str, resolver: Resolver) -> None:
    """Register a deterministic family resolver without replacing an owner."""
    key = (topic_id.strip(), proposition_family.strip())
    if not all(key) or not callable(resolver):
        raise ValueError("resolver key and callable are required")
    if key in _RESOLVERS:
        raise ValueError("a resolver is already registered for this family")
    _RESOLVERS[key] = resolver


def registered_resolvers() -> tuple[tuple[str, str], ...]:
    """Return supported deterministic resolver families for diagnostics/UI."""
    return tuple(sorted(_RESOLVERS))


def compare_contract_identities(
    left: CanonicalContractIdentity, right: CanonicalContractIdentity,
) -> dict[str, Any]:
    same = (
        left.identity_status == right.identity_status == "identified"
        and left.canonical_event_id == right.canonical_event_id
        and left.canonical_proposition_id == right.canonical_proposition_id
    )
    same_event = bool(
        left.identity_status == right.identity_status == "identified"
        and left.canonical_event_id
        and left.canonical_event_id == right.canonical_event_id
    )
    same_outcome = same and bool(left.outcome_id and right.outcome_id)
    return {
        "levels": {
            "same_topic": "confirmed" if left.topic_id and left.topic_id == right.topic_id else "unverified",
            "same_event": "confirmed" if same_event else "unverified",
            "same_proposition": "confirmed" if same else "unverified",
            "same_outcome": "confirmed" if same_outcome else "unverified",
            "semantic_match": "confirmed" if same else "unverified",
            "settlement_equivalence": "unverified",
            "economic_comparability": "unavailable",
            "executable_comparability": "unavailable",
        },
        "semantic_match": "confirmed" if same else "unverified",
        "canonical_event_id": left.canonical_event_id if same else None,
        "canonical_proposition_id": left.canonical_proposition_id if same else None,
        "settlement_equivalence": "unverified",
        "settlement_rule_hashes_equal": (
            left.settlement_rule_sha256 == right.settlement_rule_sha256
            if left.settlement_rule_sha256 and right.settlement_rule_sha256 else None
        ),
        "executable_comparison": "unavailable",
        "economic_comparability": "unavailable",
        "reason": (
            "Canonical event and outcome identity match. Matching rule hashes, "
            "even when present, do not prove equivalent settlement or cancellation handling."
            if same else "Canonical event/outcome identity is incomplete or does not match."
        ),
    }
