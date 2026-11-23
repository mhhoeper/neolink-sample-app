import asyncio
import json
import time
import tomllib
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import Cameras, Settings
from app.main import create_app
from app.replay import AppError, event_id, parse_files
from app.service import Service
import app.service as media


OUTPUT = """Files (2):
  recording with spaces  12 MB  2026-11-20T12:01:00  2026-11-20T12:01:30  subStream  md,people  [AI: people]
  older  —   2026-11-20T10:00:00  2026-11-20T10:00:15  subStream  pir
"""


def real_settings(tmp_path):
    path = tmp_path / "source.toml"
    path.write_text('''bind = "0.0.0.0"
bind_port = 8554
[[users]]
name = "private-rtsp"
pass = "private-rtsp-password"
[[cameras]]
name = "Argus 4"
username = "admin"
password = "CAMERA_SECRET"
uid = "CAMERA_UID"
discovery = "local"
stream = "both"
permitted_users = ["private-rtsp"]
[[cameras]]
name = "Garden"
username = "admin"
password = "OTHER_SECRET"
address = "192.0.2.1:9000"
stream = "main"
''', encoding="utf-8")
    return Settings(config=path, data=tmp_path / "data", demo=False)


def test_parse_real_cli_format_preserves_filename_and_metadata():
    rows = parse_files(OUTPUT, "cam", "subStream")
    assert len(rows) == 2
    assert rows[0]["name"] == "recording with spaces"
    assert rows[0]["duration"] == 30
    assert rows[0]["ai"] == ["people"]
    assert rows[1]["size"] == "—"
    assert rows[1]["tags"] == ["pir"]


@pytest.mark.parametrize("output", [
    "No connection Failed to connect",
    "Files (2):\n  file  12 MB  —  —  subStream  md\n",
    "Files (1):\n malformed",
])
def test_failures_are_not_silently_reported_as_empty(output):
    with pytest.raises(AppError):
        parse_files(output, "cam", "subStream")


def test_empty_and_missing_optional_fields():
    assert parse_files("No files for this day.\n", "cam", "subStream") == []
    row = parse_files("Files (1):\n  file  —   —  —  —  \n", "cam", "mainStream")[0]
    assert row["stream"] == "mainStream"
    assert row["tags"] == []
    assert row["duration"] is None


def test_runtime_isolates_one_camera_without_changing_original(tmp_path):
    settings = real_settings(tmp_path)
    original = settings.config.read_bytes()
    cameras = Cameras(settings)
    key = cameras.public()[0]["id"]
    runtime = tomllib.loads(cameras.runtime(key, tmp_path / "runtime", 9123).read_text())
    assert runtime["bind"] == "127.0.0.1"
    assert runtime["bind_port"] == 9123
    assert len(runtime["cameras"]) == 1
    assert runtime["cameras"][0]["password"] == "CAMERA_SECRET"
    assert runtime["cameras"][0]["discovery"] == "local"
    assert runtime["cameras"][0]["permitted_users"] == ["anonymous"]
    assert runtime["cameras"][0]["pause"]["on_motion"] is False
    assert "users" not in runtime
    assert settings.config.read_bytes() == original
    assert "CAMERA_SECRET" not in json.dumps(cameras.public())
    assert "CAMERA_UID" not in json.dumps(cameras.public())


def test_last_viewer_shutdown_and_lease_expiry(tmp_path, monkeypatch):
    async def check():
        service = Service(Settings(demo=True, data=tmp_path))
        key = service.cameras.public()[0]["id"]
        stopped = []
        async def fake_boot(live):
            live.rtsp = SimpleNamespace(name="rtsp", returncode=None)
            live.ffmpeg = SimpleNamespace(name="ffmpeg", returncode=None)
            live.state = "ready"
        async def fake_terminate(process):
            if process:
                stopped.append(process.name)
        monkeypatch.setattr(service, "boot_live", fake_boot)
        monkeypatch.setattr(media, "terminate", fake_terminate)
        first = await service.start_live(key)
        await asyncio.sleep(0)
        second = await service.start_live(key)
        assert len(service.live) == 1
        await service.stop_live(first["session"])
        assert stopped == []
        assert key in service.live
        await service.heartbeat(second["session"])
        await service.expire_leases(time.monotonic() + 50)
        assert key not in service.live
        assert stopped == ["ffmpeg", "rtsp"]
        assert not list(service.runtime.iterdir())
        await service.stop_live(second["session"])  # Idempotent.
    asyncio.run(check())


def test_cancel_during_live_start_cleans_processes(tmp_path, monkeypatch):
    async def check():
        service = Service(Settings(demo=True, data=tmp_path))
        key = service.cameras.public()[0]["id"]
        stopped = []
        async def pending_boot(live):
            live.rtsp = "starting-rtsp"
            await asyncio.Event().wait()
        async def fake_terminate(process):
            if process:
                stopped.append(process)
        monkeypatch.setattr(service, "boot_live", pending_boot)
        monkeypatch.setattr(media, "terminate", fake_terminate)
        live = await service.start_live(key)
        await asyncio.sleep(0)
        await service.stop_live(live["session"])
        assert stopped == ["starting-rtsp"]
        assert not service.live
    asyncio.run(check())


def test_persistent_event_cache_expiry_refresh_and_live_cache(tmp_path, monkeypatch):
    async def check():
        settings = real_settings(tmp_path)
        calls = []
        async def fake_run(args, timeout):
            calls.append(args)
            return OUTPUT
        monkeypatch.setattr(media, "run", fake_run)
        service = Service(settings)
        key = service.cameras.public()[0]["id"]
        result = await service.list_events(key, "2026-11-20", "subStream")
        assert not result["cached"]
        assert calls[0][-5:] == ["files", "--date", "2026-11-20", "--stream", "subStream"]
        assert len(calls) == 1
        # Recreate service to prove cache survives a restart.
        service = Service(settings)
        result = await service.list_events(key, "2026-11-20", "subStream")
        assert result["cached"] and len(calls) == 1
        assert (key, result["events"][0]["id"]) in service.events
        await service.list_events(key, "2026-11-20", "subStream", refresh=True)
        assert len(calls) == 2
        settings.event_cache_seconds = -1  # Expired cache.
        await service.list_events(key, "2026-11-20", "subStream")
        assert len(calls) == 3
        service.live[key] = object()
        result = await service.list_events(key, "2026-11-20", "subStream")
        assert result["cached"] and result["stale"]
        with pytest.raises(AppError) as caught:
            await service.list_events(key, "2026-11-20", "subStream", refresh=True)
        assert caught.value.status == 409
    asyncio.run(check())


def test_download_uses_exact_filename_complete_recording_and_persistent_video_cache(tmp_path, monkeypatch):
    async def check():
        service = Service(real_settings(tmp_path))
        key = service.cameras.public()[0]["id"]
        name = '../camera file $(echo secret)'
        eid = event_id(key, name, "subStream")
        event = {"id": eid, "name": name, "stream": "subStream"}
        service.events[(key, eid)] = event
        calls = []
        async def fake_run(args, timeout):
            calls.append(list(args))
            if "download" in args:
                Path(args[-1]).write_bytes(b"source")
            elif "-show_streams" in args:
                return '{"streams":[{"codec_type":"video","codec_name":"h264","pix_fmt":"yuv420p"}]}'
            elif "-movflags" in args:
                Path(args[-1]).write_bytes(b"playable-mp4")
            return ""
        monkeypatch.setattr(media, "run", fake_run)
        job = await service.prepare(key, eid)
        await service.jobs[eid]["task"]
        assert service.public_job(job["id"])["state"] == "ready"
        args = calls[0]
        assert args[args.index("--name") + 1] == name
        assert "--duration" not in args
        assert (service.cache / f"{eid}.mp4").read_bytes() == b"playable-mp4"
        assert not list(service.runtime.iterdir())
        service = Service(service.settings)
        service.events[(key, eid)] = event
        calls.clear()
        await service.prepare(key, eid)
        await service.jobs[eid]["task"]
        assert service.public_job(eid)["state"] == "ready"
        assert calls == []
    asyncio.run(check())


def test_subprocess_timeout_terminates_child(monkeypatch):
    async def check():
        async def communicate():
            await asyncio.Event().wait()
        fake = type("Process", (), {"communicate": staticmethod(communicate)})()
        monkeypatch.setattr(media, "spawn", AsyncMock(return_value=fake))
        cleanup = AsyncMock()
        monkeypatch.setattr(media, "terminate", cleanup)
        with pytest.raises(AppError) as caught:
            await media.run(["neolink"], .01)
        assert caught.value.status == 504
        cleanup.assert_awaited_once_with(fake)
    asyncio.run(check())


def test_api_credentials_validation_origin_and_range(tmp_path, monkeypatch):
    monkeypatch.setattr(Service, "open", AsyncMock())
    settings = real_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        result = client.get("/api/cameras")
        assert result.status_code == 200
        assert "CAMERA_SECRET" not in result.text and "CAMERA_UID" not in result.text
        key = result.json()["cameras"][0]["id"]
        assert client.get(f"/api/cameras/{key}/events?day=not-a-date").status_code == 422
        assert client.get(f"/api/cameras/{key}/events?day=2026-11-20&stream=bad").status_code == 422
        assert client.post(f"/api/cameras/{key}/live", headers={"Origin":"http://evil.example"}).status_code == 403
        filename = "a"*64 + ".mp4"
        (client.app.state.service.cache / filename).write_bytes(b"0123456789")
        response = client.get("/api/recordings/" + filename, headers={"Range":"bytes=2-5"})
        assert response.status_code == 206 and response.content == b"2345"
        assert client.get("/api/recordings/not-an-event.mp4").status_code == 404
    settings.username, settings.password = "viewer", "web-password"
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/cameras").status_code == 401
        assert client.get("/api/cameras", auth=("viewer","web-password")).status_code == 200
        assert client.get("/healthz").status_code == 200
