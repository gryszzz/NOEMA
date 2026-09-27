import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from noema.history_forecaster import MODEL_VERSION
from noema.outcomes import OutcomeStore
from noema.paper_performance import settled_paper_performance
from noema.paper_research import PaperResearchStore
from noema.paper_settlements import load_paper_settlements

NOW = datetime(2026, 9, 27, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'paper.db')
    PaperResearchStore(path).conn.close()
    OutcomeStore(path).conn.close()
    return path


def quote(path, *, market='EVENT-A', debit='.5', contracts='1', selected=1,
          model=MODEL_VERSION, offset=0, quoted=None):
    store = PaperResearchStore(path)
    at = quoted or NOW - timedelta(hours=2) + timedelta(seconds=offset)
    store.conn.execute('''INSERT INTO paper_quotes
        (venue, market_id, model_version, forecast_at, quoted_at, probability_yes,
         lower_bound, upper_bound, selected, quote_json, fee_terms_json)
        VALUES ('kalshi:demo', ?, ?, ?, ?, .9, .8, .95, ?, ?, '{}')''',
        (market, model, (at-timedelta(seconds=1)).isoformat(), at.isoformat(), selected,
         json.dumps({'total_debit_usd': debit, 'contracts': contracts})))
    store.conn.commit()
    store.conn.close()


def outcome(path, *, market='EVENT-A', seen=NOW-timedelta(hours=1), resolved=None, yes=1):
    store = OutcomeStore(path)
    store.upsert(venue='kalshi:demo', market_id=market, outcome_yes=yes,
                 resolved_at=(resolved or seen).isoformat(), seen_at=seen, raw={})
    store.conn.close()


@pytest.mark.parametrize(('field', 'value'), [
    ('debit', 'NaN'), ('debit', 'Infinity'), ('debit', '-1'), ('debit', '0'),
    ('contracts', '-1'), ('contracts', 'Infinity'), ('contracts', True),
])
def test_corrupt_paper_values_cannot_create_performance(db, field, value):
    quote(db, **{field: value})
    outcome(db)
    assert load_paper_settlements(db, now=NOW).invalid_quote_count == 1
    assert settled_paper_performance(db, now=NOW).observations == 0
    assert PaperResearchStore(db).audit(now=NOW)['market_count'] == 0


def test_repeated_market_and_other_model_cannot_multiply_strategy_returns(db):
    quote(db)
    quote(db, debit='.1', offset=1)
    quote(db, debit='.01', model='other-model')
    outcome(db)
    audit = load_paper_settlements(db, now=NOW)
    assert audit.duplicate_quote_count == 1
    assert len(audit.settlements) == 1
    assert audit.settlements[0].net_pnl_usd == Decimal('.5')
    assert settled_paper_performance(db, now=NOW).net_pnl_usd == .5
    assert Decimal(PaperResearchStore(db).audit(now=NOW)['net_pnl_usd']) == Decimal('.5')


def test_first_pass_cannot_be_replaced_by_later_winning_selection(db):
    quote(db, selected=0)
    quote(db, selected=1, offset=1)
    outcome(db)
    assert not load_paper_settlements(db, now=NOW).settlements


def test_future_and_unobserved_settlement_remain_pending(db):
    quote(db)
    outcome(db, seen=NOW+timedelta(seconds=1))
    audit = load_paper_settlements(db, now=NOW)
    assert audit.pending_quote_count == 1
    assert not audit.settlements
    assert len(load_paper_settlements(db, now=NOW+timedelta(seconds=2)).settlements) == 1


def test_drawdown_uses_actual_observation_order_across_offsets(db):
    quote(db, market='LOSS-A')
    quote(db, market='WIN-A', debit='.1')
    outcome(db, market='WIN-A', seen=NOW-timedelta(minutes=10),
            resolved=(NOW-timedelta(hours=1)).astimezone(timezone(timedelta(hours=-4))))
    outcome(db, market='LOSS-A', yes=0, seen=NOW-timedelta(minutes=20))
    performance = settled_paper_performance(db, now=NOW, starting_equity_usd=10)
    assert performance.max_drawdown_fraction == .05


@pytest.mark.parametrize('equity', [float('nan'), float('inf'), 0])
def test_drawdown_requires_finite_positive_opening_equity(db, equity):
    with pytest.raises(ValueError):
        settled_paper_performance(db, starting_equity_usd=equity)


def test_correlated_markets_are_one_return_for_significance(db):
    quote(db, market='EVENT-A', debit='.5')
    quote(db, market='EVENT-B', debit='.5')
    outcome(db, market='EVENT-A', yes=1)
    outcome(db, market='EVENT-B', yes=0)
    performance = settled_paper_performance(db, now=NOW)
    assert performance.observations == 2
    assert performance.per_trade_returns == (1.0, -1.0)
    assert performance.per_event_returns == (0.0,)
