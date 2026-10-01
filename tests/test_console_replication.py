from __future__ import annotations

import asyncio
import gzip
import sqlite3

import httpx

from noema import console_replication


def test_console_snapshot_status_distinguishes_disabled_and_misconfigured(monkeypatch, tmp_path):
    monkeypatch.delenv("NOEMA_CONSOLE_INTERNAL_ADDRESS", raising=False)
    monkeypatch.delenv("NOEMA_CONSOLE_SNAPSHOT_TOKEN", raising=False)
    assert asyncio.run(console_replication.publish_console_snapshot(str(tmp_path / "missing.db"))) == {
        "status": "disabled",
    }

    monkeypatch.setenv("NOEMA_CONSOLE_INTERNAL_ADDRESS", "console.internal:10000")
    assert asyncio.run(console_replication.publish_console_snapshot(str(tmp_path / "missing.db"))) == {
        "status": "misconfigured", "failure_stage": "configuration",
    }


def test_console_snapshot_local_backup_failure_does_not_include_path(monkeypatch, tmp_path):
    monkeypatch.setenv("NOEMA_CONSOLE_INTERNAL_ADDRESS", "console.internal:10000")
    monkeypatch.setenv("NOEMA_CONSOLE_SNAPSHOT_TOKEN", "snapshot-secret-placeholder")

    result = asyncio.run(console_replication.publish_console_snapshot(str(tmp_path / "private-path.db")))

    assert result["status"] == "unavailable"
    assert result["failure_stage"] == "snapshot_backup"
    assert result["failure_classification"] == "worker_snapshot_storage_failure"
    assert result["error_type"] == "FileNotFoundError"
    assert "private-path" not in repr(result)
    assert "snapshot-secret-placeholder" not in repr(result)


def test_console_snapshot_http_rejection_reports_status_without_response_body(
    monkeypatch, tmp_path,
):
    database = tmp_path / "worker.db"
    with sqlite3.connect(database):
        pass
    monkeypatch.setenv("NOEMA_CONSOLE_INTERNAL_ADDRESS", "console.internal:10000")
    monkeypatch.setenv("NOEMA_CONSOLE_SNAPSHOT_TOKEN", "snapshot-secret-placeholder")
    monkeypatch.setattr(console_replication, "_worker_metadata_header", lambda: "safe-metadata")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url, *, content, headers):
            assert method == "POST"
            assert url == "http://console.internal:10000/internal/snapshot"
            assert headers["Authorization"] == "Bearer snapshot-secret-placeholder"

            class Response:
                async def __aenter__(self):
                    payload = bytearray()
                    async for chunk in content:
                        payload.extend(chunk)
                    assert len(payload) == int(headers["Content-Length"])
                    assert gzip.decompress(payload).startswith(b"SQLite format 3")
                    return httpx.Response(
                        401, request=httpx.Request("POST", url), text="sensitive response body",
                    )

                async def __aexit__(self, *_args):
                    return None

            return Response()

    monkeypatch.setattr(console_replication.httpx, "AsyncClient", lambda **_kwargs: Client())
    result = asyncio.run(console_replication.publish_console_snapshot(str(database)))

    assert result == {
        "status": "unavailable",
        "failure_stage": "private_console_post",
        "failure_classification": "private_console_http_rejection",
        "error_type": "HTTPStatusError",
        "http_status": 401,
    }
    assert "sensitive response body" not in repr(result)
    assert "snapshot-secret-placeholder" not in repr(result)
