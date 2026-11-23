import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.config import Settings
from app.main import create_app
from app.replay import AppError
from app.service import Service
from test_app import OUTPUT, real_settings
import app.service as media


def test_combined_query_uses_each_camera_stream_and_keeps_identity(tmp_path, monkeypatch):
    async def check():
        service = Service(real_settings(tmp_path))
        calls = []
        async def fake_run(args, timeout):
            calls.append(args)
            return OUTPUT.replace("subStream", args[-1])
        monkeypatch.setattr(media, "run", fake_run)
        result = await service.list_all_events("2026-11-20")
        assert len(result["events"]) == 4 and not result["partial"]
        assert {camera["name"]: camera["stream"] for camera in result["cameras"]} == {
            "Argus 4": "subStream", "Garden": "mainStream"}
        assert [row["start"] for row in result["events"]] == sorted(
            [row["start"] for row in result["events"]], reverse=True)
        assert len({row["id"] for row in result["events"]}) == 4
        for row in result["events"]:
            assert service.cameras.get(row["camera_id"])["name"] == row["camera_name"]
            assert (row["camera_id"], row["id"]) in service.events
        await service.list_all_events("2026-11-20")
        assert len(calls) == 2
        result = await service.list_all_events("2026-11-20", "mainStream", True)
        assert all(camera["stream"] == "mainStream" for camera in result["cameras"])
    asyncio.run(check())


def test_partial_failure_returns_other_camera_and_stale_cache(tmp_path, monkeypatch):
    async def check():
        service = Service(real_settings(tmp_path))
        failure = False
        async def fake_run(args, timeout):
            if failure and args[4] == "Garden":
                raise AppError("Camera offline", 504)
            return OUTPUT
        monkeypatch.setattr(media, "run", fake_run)
        await service.list_all_events("2026-11-20")
        failure = True
        result = await service.list_all_events("2026-11-20", refresh=True)
        assert result["partial"] and len(result["events"]) == 4
        garden = next(row for row in result["cameras"] if row["name"] == "Garden")
        assert garden["state"] == "stale" and garden["error"] == "Camera offline"
        assert garden["updated_at"]
        for path in service.event_cache.glob(f'{garden["id"]}-*.json'):
            path.unlink()
        result = await service.list_all_events("2026-11-20", refresh=True)
        assert len(result["events"]) == 2 and result["partial"]
        assert next(row for row in result["cameras"] if row["name"] == "Garden")["state"] == "error"
    asyncio.run(check())


def test_combined_queries_are_bounded_and_all_errors_remain_visible(tmp_path, monkeypatch):
    async def check():
        service = Service(Settings(demo=True, data=tmp_path))
        current = peak = 0
        async def failed(*args):
            nonlocal current, peak
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(.01)
            current -= 1
            raise AppError("Offline")
        monkeypatch.setattr(service, "list_events", failed)
        result = await service.list_all_events("2026-11-20")
        assert peak == 2
        assert result["events"] == [] and result["partial"]
        assert len(result["cameras"]) == 3
        assert all(row["state"] == "error" for row in result["cameras"])
    asyncio.run(check())


def test_selected_recording_survives_registry_eviction_and_restart(tmp_path, monkeypatch):
    async def check():
        settings = Settings(demo=True, data=tmp_path)
        service = Service(settings)
        result = await service.list_all_events("2026-11-20")
        row = result["events"][0]
        service._remember_events(row["camera_id"], [{"id": str(i)} for i in range(10001)])
        assert len(service.events) == 10000
        assert (row["camera_id"], row["id"]) not in service.events
        # A new process also starts with no in-memory metadata.
        service = Service(settings)
        download = AsyncMock()
        monkeypatch.setattr(service, "download", download)
        await service.prepare(row["camera_id"], row["id"])
        await service.jobs[row["id"]]["task"]
        assert download.await_args.args[0] == row["camera_id"]
        assert download.await_args.args[1]["name"] == row["name"]
        other = next(camera["id"] for camera in service.cameras.public() if camera["id"] != row["camera_id"])
        with pytest.raises(AppError) as caught:
            await service.prepare(other, row["id"])
        assert caught.value.status == 404
    asyncio.run(check())


def test_combined_api_validates_input_and_returns_camera_metadata(tmp_path):
    with TestClient(create_app(Settings(demo=True, data=tmp_path))) as client:
        assert client.get("/api/events?day=bad").status_code == 422
        assert client.get("/api/events?day=2026-11-20&stream=bad").status_code == 422
        result = client.get("/api/events?day=2026-11-20").json()
        assert len(result["cameras"]) == 3 and not result["partial"]
        assert len({row["camera_id"] for row in result["events"]}) == 3
