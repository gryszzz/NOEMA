from noema import resource_control


def test_memory_pressure_blocks_heavy_work(monkeypatch, tmp_path):
    monkeypatch.setenv("NOEMA_RESOURCE_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("NOEMA_MIN_AVAILABLE_MEMORY_PERCENT", "25")
    monkeypatch.setattr(resource_control, "memory_snapshot", lambda: {
        "available_percent": 12, "source": "test",
    })
    lease, reason = resource_control.try_acquire("docker_worker")
    assert lease is None
    assert reason.startswith("RESOURCE LIMITED")
    assert resource_control.resource_status()["state"] == "resource_limited"


def test_heavy_work_slot_is_singleton_and_releases(monkeypatch, tmp_path):
    monkeypatch.setenv("NOEMA_RESOURCE_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(resource_control, "memory_snapshot", lambda: {
        "available_percent": 70, "source": "test",
    })
    first, reason = resource_control.try_acquire("docker_worker")
    assert first is not None and reason is None
    second, reason = resource_control.try_acquire("docker_worker")
    assert second is None
    assert reason.startswith("QUEUED")
    assert resource_control.resource_status()["slots"]["docker_worker"] == "working"
    first.release()
    third, reason = resource_control.try_acquire("docker_worker")
    assert third is not None and reason is None
    third.release()


def test_resource_kind_is_allowlisted():
    try:
        resource_control.try_acquire("arbitrary")
    except ValueError as exc:
        assert str(exc) == "unknown resource class"
    else:
        raise AssertionError("unrecognized resource class must fail closed")
