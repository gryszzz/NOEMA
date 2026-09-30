import sqlite3
from datetime import UTC, datetime, timedelta

from noema.live_balance import balance_history, project_balance

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)


def sources(now=NOW, amount="20"):
    return ({"as_of": now.isoformat(), "venues": [{"venue": "Kalshi", "account": {
        "status": "authenticated_read_only", "cash_balance_usd": amount}}]},
        {"observed_at": now.isoformat(), "networks": [{"chain": "solana", "address": "fixture",
           "readable": True, "native_value_usd": "12.50"}]})


def test_one_total_preserves_scope_and_unknowns():
    venues, wallets = sources()
    result = project_balance(venues, wallets, NOW)
    assert result["amount_usd"] == "32.50"
    assert result["valued_sources"] == 2
    assert result["expected_sources"] == 8
    assert result["is_profit"] is False
    assert result["status"] == "CACHED"
    assert "open positions" in result["exclusions"]
    assert project_balance({}, {}, NOW)["amount_usd"] is None
    assert project_balance(venues, wallets, NOW + timedelta(minutes=3))["status"] == "UNKNOWN"
    assert project_balance(venues, wallets, NOW - timedelta(seconds=1))["status"] == "UNKNOWN"
    venues["venues"][0]["account"]["cash_balance_usd"] = "NaN"
    assert project_balance(venues, wallets, NOW)["amount_usd"] == "12.50"


def test_history_is_immutable_deduplicated_and_survives_reload(tmp_path):
    db = tmp_path / "capital.db"
    sqlite3.connect(db).close()
    venues, wallets = sources()
    first = balance_history(str(db), venues, wallets, now=NOW)
    second = balance_history(str(db), venues, wallets, now=NOW + timedelta(seconds=10))
    assert first["points"] == second["points"]
    assert second["sample_count"] == 1
    assert second["change_24h_usd"] is None
    later = NOW + timedelta(days=1)
    venues, wallets = sources(later, "25")
    third = balance_history(str(db), venues, wallets, window="ALL", now=later)
    assert third["sample_count"] == 2
    assert third["points"][0]["amount_usd"] == "32.50"
    assert third["change_24h_usd"] == "5.00"
    assert balance_history(str(db), venues, wallets, window="1H", now=later)["sample_count"] == 1
    wallets["networks"][0]["address"] = "another-account"
    changed = balance_history(str(db), venues, wallets, now=later)
    assert changed["change_24h_usd"] is None


def test_stale_reads_do_not_extend_history_or_fabricate_zero(tmp_path):
    db = tmp_path / "capital.db"
    assert balance_history(str(db), {}, {}, now=NOW)["history_status"] == "unavailable"
    assert not db.exists()
    sqlite3.connect(db).close()
    venues, wallets = sources()
    result = balance_history(str(db), venues, wallets, now=NOW + timedelta(minutes=3))
    assert result["sample_count"] == 0
    assert result["current"]["status"] == "UNKNOWN"


def test_native_wallets_and_only_available_live_stripe_funds_roll_into_one_total():
    venues, wallets = sources()
    stripe = {"status": "connected", "livemode": True, "observed_at": NOW.isoformat(),
              "available": [{"currency": "usd", "amount_minor": 303}],
              "pending": [{"currency": "usd", "amount_minor": 900000}],
              "successful_usd_minor": 8000000}
    result = project_balance(venues, wallets, NOW, stripe)
    assert result["amount_usd"] == "35.53"
    from decimal import Decimal
    assert sum(Decimal(row["amount_usd"]) for row in result["contributions"] if row["included"]) == Decimal(result["amount_usd"])
    stripe["livemode"] = False
    assert project_balance(venues, wallets, NOW, stripe)["amount_usd"] == "32.50"
    stripe["livemode"] = True
    stripe["observed_at"] = (NOW - timedelta(hours=1)).isoformat()
    stale = project_balance(venues, wallets, NOW, stripe)
    assert stale["amount_usd"] == "32.50"
    assert stale["contributions"][-1]["status"] == "STALE"
    assert stale["stale_sources"] == 1


def test_funded_unpriced_is_never_zero_and_chain_accounts_remain_distinct():
    venues, wallets = sources()
    wallets["networks"][0].update(native_value_usd=None, funded=True)
    wallets["networks"].extend([
        {"chain": "base", "address": "same", "native_value_usd": "9.97", "readable": True},
        {"chain": "ethereum", "address": "same", "native_value_usd": "0", "readable": True},
    ])
    result = project_balance(venues, wallets, NOW)
    sol = next(row for row in result["contributions"] if row["label"] == "solana")
    assert sol["status"] == "UNPRICED"
    assert sol["funded"] is True
    assert sol["amount_usd"] is None
    assert result["amount_usd"] == "29.97"
    assert result["unpriced_sources"] == 1


def test_native_total_remains_known_while_unpriced_tokens_are_counted_separately():
    venues, wallets = sources()
    wallets["networks"][0]["tokens"] = [
        {"symbol": "ABC", "raw_amount": "1000000000000000000", "decimals": 18},
        {"symbol": "ZERO", "raw_amount": "0", "decimals": 18},
    ]
    result = project_balance(venues, wallets, NOW)
    assert result["amount_usd"] == "32.50"
    assert result["unpriced_assets"] == 1


def test_history_exposes_source_contributions_for_auditable_chart_points(tmp_path):
    db = tmp_path / "capital.db"
    sqlite3.connect(db).close()
    venues, wallets = sources()
    result = balance_history(str(db), venues, wallets, now=NOW)
    point = result["points"][0]
    assert sum(row["included"] for row in point["contributions"]) == 2
    assert {row["id"] for row in point["contributions"] if row["included"]} == {
        "venue:Kalshi", "wallet:solana:fixture",
    }
