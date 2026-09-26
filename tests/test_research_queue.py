from noema.research_queue import ResearchQueueStore


def test_research_queue_deduplicates_and_keeps_highest_priority(tmp_path) -> None:
    store = ResearchQueueStore(str(tmp_path / "noema.db"))
    store.enqueue(market_id="M", request="Verify source", priority=0.4)
    store.enqueue(market_id="M", request="Verify source", priority=0.8)
    tasks = store.pending()
    assert len(tasks) == 1
    assert tasks[0].priority == 0.8
