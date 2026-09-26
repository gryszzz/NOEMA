from noema.realtime_journal import RealtimeJournal


def test_journal_hashes_and_persists_event(tmp_path) -> None:
    journal = RealtimeJournal(str(tmp_path / "noema.db"))
    event = journal.append(
        channel="ticker",
        ticker="M",
        sequence=1,
        payload={"price": 0.5},
    )
    assert len(event.payload_hash) == 64

    row = journal.conn.execute(
        "SELECT channel, ticker, sequence FROM realtime_events"
    ).fetchone()
    assert row == ("ticker", "M", 1)
