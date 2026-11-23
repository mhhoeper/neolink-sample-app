from email.message import Message
import asyncio
from datetime import datetime
from urllib.parse import unquote
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.replay import AppError, event_id
from app.service import Service
import app.service as media


def test_recent_window_includes_yesterday_and_overlap_but_excludes_future(tmp_path, monkeypatch):
    async def check():
        service = Service(Settings(demo=True, data=tmp_path))
        calls = []
        async def lists(camera, day, stream, refresh):
            calls.append((camera, day, refresh))
            times = [
                ('old', '2026-11-22T11:00:00', '2026-11-22T11:01:00'),
                ('overlap', '2026-11-22T11:59:50', '2026-11-22T12:00:20'),
                ('yesterday', '2026-11-22T12:00:00', '2026-11-22T12:00:30'),
                ('today', '2026-11-23T11:00:00', '2026-11-23T11:00:30'),
                ('future', '2026-11-23T12:00:00', '2026-11-23T12:00:30'),
                ('unknown', None, None),
            ]
            return {'events': [{'id':event_id(camera,name,stream), 'name':name,
                                'start':start,'end':end,'stream':stream} for name,start,end in times],
                    'updated_at':100, 'cached':False}
        monkeypatch.setattr(service, 'list_events', lists)
        result = await service.list_recent_events(datetime(2026,11,23,12), refresh=True)
        assert result['window'] == {'start':'2026-11-22T12:00:00','end':'2026-11-23T12:00:00'}
        assert {row['name'] for row in result['events']} == {'overlap','yesterday','today','unknown'}
        assert len(result['events']) == 12  # Duplicate IDs from both dates are merged.
        assert all(camera['event_count']==4 for camera in result['cameras'])
        for camera in service.cameras.public():
            assert [(day, refresh) for key,day,refresh in calls if key==camera['id']] == [
                ('2026-11-22',True), ('2026-11-23',True)]
    asyncio.run(check())


def test_missing_half_window_is_partial_not_an_empty_camera(tmp_path, monkeypatch):
    async def check():
        service = Service(Settings(demo=True, data=tmp_path))
        original = service.list_events
        async def lists(camera, day, stream, refresh):
            if day == '2026-11-22':
                raise AppError('Camera offline')
            return await original(camera, day, stream, refresh)
        monkeypatch.setattr(service, 'list_events', lists)
        result = await service.list_recent_events(datetime(2026,11,23,20))
        assert result['partial'] and len(result['events']) == 15
        assert all(camera['state']=='partial' and camera['event_count']==5 for camera in result['cameras'])
        assert all('2026-11-22' in camera['error'] for camera in result['cameras'])
    asyncio.run(check())


def test_recent_refresh_uses_persistent_caches_when_cameras_fail(tmp_path, monkeypatch):
    async def check():
        settings = Settings(demo=True, data=tmp_path)
        service = Service(settings)
        expected = await service.list_recent_events(datetime(2026,11,23,12))
        service = Service(settings)
        monkeypatch.setattr(service, 'list_events', AsyncMock(side_effect=AppError('Offline')))
        result = await service.list_recent_events(datetime(2026,11,23,12), refresh=True)
        assert result['events'] == expected['events']
        assert result['partial'] and all(camera['state']=='stale' for camera in result['cameras'])
    asyncio.run(check())


def test_recent_api_validates_local_time_and_returns_exact_window(tmp_path):
    with TestClient(create_app(Settings(demo=True, data=tmp_path))) as client:
        for query in ['', '?until=bad', '?until=2026-11-23T12:00:00Z',
                      '?until=2026-11-23T12:00:00&day=2026-11-23', '?until=0001-01-01T00:00:00']:
            assert client.get('/api/events'+query).status_code == 422
        result = client.get('/api/events?until=2026-11-23T12:00:00').json()
        assert result['day'] == '2026-11-23'
        assert result['window']['start'] == '2026-11-22T12:00:00'
        assert result['window']['end'] == '2026-11-23T12:00:00'
        assert any(row['start'].startswith('2026-11-22') for row in result['events'])
        assert any(row['start'].startswith('2026-11-23') for row in result['events'])


def test_download_returns_full_cached_recording_with_name_and_no_camera_connection(tmp_path, monkeypatch):
    settings = Settings(demo=True, data=tmp_path)
    with TestClient(create_app(settings)) as client:
        result = client.get('/api/events?day=2026-11-23').json()
        row = next(row for row in result['events'] if row['camera_name']=='Front door')
        service = client.app.state.service
        path = service.cache / (row['id']+'.mp4')
        path.write_bytes(b'complete-recording-content')
        service.events.clear()  # Resolve ownership through persisted metadata.
        runner = AsyncMock(side_effect=AssertionError('No camera request is needed for a download'))
        monkeypatch.setattr(media, 'run', runner)
        url = f'/api/cameras/{row["camera_id"]}/events/{row["id"]}/download'
        response = client.get(url)
        assert response.status_code == 200 and response.content == path.read_bytes()
        disposition = unquote(response.headers['content-disposition'])
        assert disposition.startswith('attachment;')
        header = Message()
        header['Content-Disposition'] = disposition
        filename = header.get_filename()
        assert 'Front-door_2026-11-23_' in filename and filename.endswith('.mp4')
        assert response.headers['content-type'] == 'video/mp4'
        runner.assert_not_called()
        other = next(camera['id'] for camera in result['cameras'] if camera['id'] != row['camera_id'])
        assert client.get(f'/api/cameras/{other}/events/{row["id"]}/download').status_code == 404
        path.unlink()
        assert client.get(url).status_code == 404
        assert client.get(f'/api/cameras/{row["camera_id"]}/events/invalid/download').status_code == 404
    settings.username, settings.password = 'viewer', 'test-password'
    with TestClient(create_app(settings)) as client:
        assert client.get(url).status_code == 401
