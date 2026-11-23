"""Supervise Neolink/FFmpeg and release camera connections when viewers stop."""
import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from .config import Cameras, Settings
from .replay import AppError, demo_events, parse_files

log = logging.getLogger("neolink.web")


async def terminate(process):
    if process is None or process.returncode is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        await asyncio.wait_for(process.wait(), 4)
    except asyncio.TimeoutError:
        if os.name != "nt":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await process.wait()
    except ProcessLookupError:
        await process.wait()


async def spawn(args, capture=False):
    options = {"start_new_session": True} if os.name != "nt" else {}
    return await asyncio.create_subprocess_exec(
        *map(str, args), stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, **options,
    )


async def run(args, timeout):
    process = await spawn(args, capture=True)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode != 0:
            log.warning("Media subprocess failed (exit %s)", process.returncode)
            raise AppError("Camera query or video processing failed. Check the connection and configuration.")
        return output.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        raise AppError("Camera query or video processing timed out.", 504)
    finally:
        await terminate(process)


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Live:
    camera_id: str
    key: str
    directory: Path
    leases: dict = field(default_factory=dict)
    state: str = "starting"
    error: str | None = None
    rtsp: object = None
    ffmpeg: object = None
    task: object = None


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cameras = Cameras(settings)
        self.live = {}
        self.locks = {key: asyncio.Lock() for key in self.cameras.entries}
        self.events = {}
        self.jobs = {}
        self.closing = False
        self.reaper = None
        self.workers = asyncio.Semaphore(2)
        self.event_workers = asyncio.Semaphore(2)
        settings.data.mkdir(parents=True, exist_ok=True)
        self.cache = settings.data / "recordings"
        self.runtime = settings.data / "runtime"
        self.cache.mkdir(exist_ok=True)
        self.event_cache = settings.data / "events"
        self.event_cache.mkdir(exist_ok=True)
        # A single server worker owns runtime files and processes.
        shutil.rmtree(self.runtime, ignore_errors=True)
        self.runtime.mkdir(mode=0o700)

    async def open(self):
        if not self.settings.demo:
            for binary in (self.settings.neolink, self.settings.ffmpeg, self.settings.ffprobe):
                if not shutil.which(binary):
                    raise RuntimeError(f"Required program is missing: {binary}")
            try:
                await run([self.settings.neolink, "replay", "--help"], 15)
            except AppError as exc:
                raise RuntimeError("This Neolink image must support the replay subcommand.") from exc
        self.reaper = asyncio.create_task(self.reap())

    def camera(self, camera_id):
        if camera_id not in self.cameras.entries:
            raise AppError("Camera not found.", 404)

    def live_public(self, live, lease):
        return {"session": lease, "state": live.state, "error": live.error,
                "url": f"/api/live/{lease}/index.m3u8"}

    def find_live(self, lease):
        for live in self.live.values():
            if lease in live.leases:
                return live
        raise AppError("Live view has ended. Start it again.", 410)

    async def start_live(self, camera_id):
        self.camera(camera_id)
        self.cameras.stream(camera_id)  # Also validates disabled streams.
        lock = self.locks[camera_id]
        if lock.locked():
            raise AppError("This camera is being queried. Please wait.", 409)
        async with lock:
            live = self.live.get(camera_id)
            if live and live.state == "error":
                await self.stop_locked(live)
                live = None
            if live is None:
                key = secrets.token_hex(16)
                live = Live(camera_id, key, self.runtime / key)
                live.directory.mkdir(mode=0o700)
                self.live[camera_id] = live
                live.task = asyncio.create_task(self.boot_live(live))
            lease = secrets.token_urlsafe(24)
            live.leases[lease] = time.monotonic()
            return self.live_public(live, lease)

    async def heartbeat(self, lease):
        live = self.find_live(lease)
        live.leases[lease] = time.monotonic()
        if live.state == "ready" and live.ffmpeg and live.ffmpeg.returncode is not None:
            live.state, live.error = "error", "The camera stream was interrupted. Start it again."
            await terminate(live.rtsp)
        return self.live_public(live, lease)

    async def stop_live(self, lease):
        try:
            live = self.find_live(lease)
        except AppError:
            return
        async with self.locks[live.camera_id]:
            live.leases.pop(lease, None)
            if not live.leases:
                await self.stop_locked(live)

    async def stop_locked(self, live):
        if live.task and not live.task.done():
            live.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await live.task
        await terminate(live.ffmpeg)
        await terminate(live.rtsp)
        shutil.rmtree(live.directory, ignore_errors=True)
        if self.live.get(live.camera_id) is live:
            del self.live[live.camera_id]

    async def boot_live(self, live):
        try:
            deadline = time.monotonic() + self.settings.startup_seconds
            if self.settings.demo:
                args = [self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-re",
                        "-f", "lavfi", "-i", "testsrc2=size=960x540:rate=15"]
                codec = "transcode"
            else:
                port = unused_port()
                config = self.cameras.runtime(live.camera_id, live.directory, port)
                live.rtsp = await spawn([self.settings.neolink, "--config", config, "rtsp"])
                while True:
                    if live.rtsp.returncode is not None:
                        raise AppError("Neolink could not start RTSP mode.")
                    try:
                        reader, writer = await asyncio.wait_for(
                            asyncio.open_connection("127.0.0.1", port), 1
                        )
                        writer.close()
                        await writer.wait_closed()
                        break
                    except (OSError, asyncio.TimeoutError):
                        if time.monotonic() >= deadline:
                            raise AppError("The camera did not become available for live view in time.", 504)
                        await asyncio.sleep(0.4)
                stream = self.cameras.stream(live.camera_id)
                url = f"rtsp://127.0.0.1:{port}/{quote(self.cameras.get(live.camera_id)['name'], safe='')}/{stream}"
                codec = os.getenv("LIVE_VIDEO_MODE", "auto")
                if codec not in ("auto", "transcode"):
                    raise AppError("LIVE_VIDEO_MODE must be auto or transcode.")
                if codec == "auto":
                    try:
                        probe = json.loads(await run([
                            self.settings.ffprobe, "-v", "error", "-rtsp_transport", "tcp",
                            "-timeout", "15000000", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name,pix_fmt", "-of", "json", url,
                        ], 20))
                        video = probe.get("streams", [{}])[0]
                        codec = "copy" if video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p" else "transcode"
                    except (AppError, ValueError, IndexError):
                        codec = "transcode"
                args = [self.settings.ffmpeg, "-hide_banner", "-loglevel", "error",
                        "-rtsp_transport", "tcp", "-timeout", "15000000", "-i", url]
            output = ["-map", "0:v:0", "-map", "0:a?", "-c:a", "aac", "-b:a", "96k"]
            if codec == "copy":
                output += ["-c:v", "copy"]
            else:
                output += ["-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
                           "-vf", "scale=w='min(1280,iw)':h=-2", "-pix_fmt", "yuv420p",
                           "-r", "15", "-g", "15", "-keyint_min", "15", "-sc_threshold", "0"]
            output += ["-f", "hls", "-hls_time", "1", "-hls_list_size", "6",
                       "-hls_flags", "delete_segments+independent_segments+temp_file",
                       "-hls_segment_filename", live.directory / "segment-%06d.ts",
                       live.directory / "index.m3u8"]
            while time.monotonic() < deadline:
                live.ffmpeg = await spawn(args + output)
                while live.ffmpeg.returncode is None and time.monotonic() < deadline:
                    playlist = live.directory / "index.m3u8"
                    if playlist.exists() and "#EXTINF" in playlist.read_text():
                        live.state = "ready"
                        return
                    await asyncio.sleep(0.4)
                await terminate(live.ffmpeg)
                await asyncio.sleep(0.7)
            raise AppError("No live video was received. Check the camera and stream.", 504)
        except asyncio.CancelledError:
            await terminate(live.ffmpeg)
            await terminate(live.rtsp)
            raise
        except Exception as exc:
            live.state = "error"
            live.error = exc.message if isinstance(exc, AppError) else "Live view could not be started."
            log.warning("Live startup failed (%s)", type(exc).__name__)
            await terminate(live.ffmpeg)
            await terminate(live.rtsp)

    def _read_event_cache(self, path):
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cached.get("events"), list) and isinstance(cached.get("updated_at"), (float, int)):
                return cached
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _remember_events(self, camera_id, rows):
        for row in rows:
            key = (camera_id, row["id"])
            self.events.pop(key, None)
            self.events[key] = row
        while len(self.events) > 10000:
            self.events.pop(next(iter(self.events)))

    async def list_all_events(self, day, stream="auto", refresh=False):
        return await self._collect_events([day], stream, refresh)

    async def list_recent_events(self, until, stream="auto", refresh=False):
        """A moving 24-hour window in the camera's local wall clock."""
        if until.tzinfo is not None:
            raise AppError("Use camera-local time without a timezone offset.", 422)
        until = until.replace(microsecond=0)
        try:
            since = until - timedelta(hours=24)
        except OverflowError as exc:
            raise AppError("Invalid end of the 24-hour window.", 422) from exc
        days = [since.date().isoformat(), until.date().isoformat()]
        result = await self._collect_events(days, stream, refresh)
        def intersects(row):
            try:
                start = datetime.fromisoformat(row["start"])
                end = datetime.fromisoformat(row["end"]) if row.get("end") else (
                    start + timedelta(seconds=row.get("duration") or 0))
                return start < until and (end > since or start >= since)
            except (ValueError, TypeError, KeyError):
                # Unknown camera times remain playable in the list.
                return True
        result["events"] = [row for row in result["events"] if intersects(row)]
        for camera in result["cameras"]:
            camera["event_count"] = sum(row["camera_id"] == camera["id"] for row in result["events"])
        return {**result, "day": until.date().isoformat(),
                "window": {"start": since.isoformat(), "end": until.isoformat()}}

    async def _collect_events(self, days, stream, refresh):
        """Combine cameras without turning a partial failure into an empty day."""
        async def collect(camera):
            camera_id = camera["id"]
            camera_stream = stream
            if stream == "auto":
                configured = self.cameras.get(camera_id).get("stream", "both").lower()
                camera_stream = "mainStream" if configured in ("main", "mainstream") else "subStream"
            results, errors, statuses = [], [], []
            # One camera's dates are queried sequentially; at most two cameras
            # connect at once. Parallel dates would contend for the same lock.
            async with self.event_workers:
                for day in days:
                    try:
                        result = await self.list_events(camera_id, day, camera_stream, refresh)
                        status = "stale" if result.get("stale") else "cached" if result.get("cached") else "ready"
                    except Exception as exc:
                        log.warning("Combined camera query failed (%s)", type(exc).__name__)
                        error = exc.message if isinstance(exc, AppError) else "The camera could not be queried."
                        errors.append((day + ": " if len(days) > 1 else "") + error)
                        path = self.event_cache / f"{camera_id}-{day}-{camera_stream}.json"
                        cached = self._read_event_cache(path)
                        if cached:
                            self._remember_events(camera_id, cached["events"])
                            result = {**cached, "cached": True, "stale": True}
                            status = "stale"
                        else:
                            result = {"events": [], "updated_at": None}
                            status = "error"
                    results.append(result)
                    statuses.append(status)
            if all(status == "error" for status in statuses):
                status = "error"
            elif "error" in statuses:
                status = "partial"
            elif "stale" in statuses:
                status = "stale"
            else:
                status = "cached" if all(s == "cached" for s in statuses) else "ready"
            unique = {row["id"]: row for result in results for row in result["events"]}
            events = [{**row, "camera_id": camera_id, "camera_name": camera["name"]} for row in unique.values()]
            stamps = [result["updated_at"] for result in results if result.get("updated_at") is not None]
            state = {**camera, "state": status, "event_count": len(events),
                     "updated_at": min(stamps) if stamps else None,
                     "error": " · ".join(errors) or None, "stream": camera_stream}
            return events, state

        results = await asyncio.gather(*(collect(camera) for camera in self.cameras.public()))
        events = [row for rows, _ in results for row in rows]
        events.sort(key=lambda row: (row.get("start") or "", row["camera_name"]), reverse=True)
        states = [state for _, state in results]
        return {"events": events, "cameras": states,
                "partial": any(state["state"] in ("error", "stale", "partial") for state in states)}

    async def list_events(self, camera_id, day, stream, refresh=False):
        self.camera(camera_id)
        key = f"{camera_id}-{day}-{stream}"
        cache_path = self.event_cache / f"{key}.json"
        cached = self._read_event_cache(cache_path)
        if cached and not refresh:
            age = time.time() - cached["updated_at"]
            if age < self.settings.event_cache_seconds or camera_id in self.live:
                self._remember_events(camera_id, cached["events"])
                return {**cached, "cached": True, "stale": age >= self.settings.event_cache_seconds}
        if camera_id in self.live:
            raise AppError("Stop live view for this camera before querying events.", 409)
        if self.locks[camera_id].locked():
            raise AppError("This camera is being queried. Please wait.", 409)
        async with self.locks[camera_id]:
            if self.settings.demo:
                offset = list(self.cameras.entries).index(camera_id) * 45
                rows = demo_events(camera_id, day, stream, offset)
            else:
                directory = self.runtime / secrets.token_hex(16)
                try:
                    config = self.cameras.runtime(camera_id, directory)
                    output = await run([
                        self.settings.neolink, "--config", config, "replay",
                        self.cameras.get(camera_id)["name"], "files",
                        "--date", day, "--stream", stream,
                    ], self.settings.query_seconds)
                    rows = parse_files(output, camera_id, stream)
                finally:
                    shutil.rmtree(directory, ignore_errors=True)
            self._remember_events(camera_id, rows)
            result = {"events": rows, "updated_at": time.time()}
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cache_path)
            cached_lists = sorted(self.event_cache.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for old in cached_lists[:-1000]:
                old.unlink(missing_ok=True)
            return {**result, "cached": False, "stale": False}

    def find_event(self, camera_id, event_id):
        self.camera(camera_id)
        event = self.events.get((camera_id, event_id))
        if event is None:
            # Large merged days can evict metadata from memory. The persisted
            # lists still authenticate which filename belongs to this camera.
            paths = sorted(self.event_cache.glob(f"{camera_id}-*.json"),
                           key=lambda path: path.stat().st_mtime, reverse=True)
            for path in paths:
                cached = self._read_event_cache(path)
                if cached:
                    event = next((row for row in cached["events"] if row["id"] == event_id), None)
                    if event:
                        self._remember_events(camera_id, [event])
                        break
        if event is None:
            raise AppError("Event not found. Reload the list.", 404)
        return event

    async def prepare(self, camera_id, event_id):
        event = self.find_event(camera_id, event_id)
        if camera_id in self.live:
            raise AppError("Stop live view for this camera first.", 409)
        job = self.jobs.get(event_id)
        if job and job["state"] in ("queued", "downloading", "converting", "ready"):
            if job["state"] != "ready" or (self.cache / f"{event_id}.mp4").exists():
                return self.public_job(event_id)
        job = {"state": "queued", "error": None, "camera": camera_id, "updated": time.monotonic()}
        self.jobs[event_id] = job
        job["task"] = asyncio.create_task(self.download(camera_id, event, job))
        return self.public_job(event_id)

    def public_job(self, key):
        job = self.jobs.get(key)
        if job is None:
            raise AppError("Playback job not found.", 404)
        return {"id": key, "state": job["state"], "error": job["error"],
                "url": f"/api/recordings/{key}.mp4" if job["state"] == "ready" else None}

    async def download(self, camera_id, event, job):
        directory = self.runtime / secrets.token_hex(16)
        final = self.cache / f"{event['id']}.mp4"
        temporary = self.cache / f"{event['id']}.part.mp4"
        try:
            async with self.workers, self.locks[camera_id]:
                if camera_id in self.live:
                    raise AppError("The camera is streaming live video. Stop live view first.", 409)
                directory.mkdir(mode=0o700)
                if not final.exists():
                    job["state"] = "downloading"
                    source = directory / "download.mp4"
                    if self.settings.demo:
                        await run([self.settings.ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                                   "testsrc2=size=960x540:rate=15", "-t", str(event.get("duration") or 18), "-c:v", "libx264",
                                   "-threads", "2", "-pix_fmt", "yuv420p", source], 40)
                    else:
                        config = self.cameras.runtime(camera_id, directory)
                        await run([self.settings.neolink, "--config", config, "replay",
                                   self.cameras.get(camera_id)["name"], "download",
                                   "--name", event["name"], "--stream", event["stream"],
                                   "--output", source], self.settings.download_seconds)
                    candidates = [source, source.with_suffix(".h264"), source.with_suffix(".h265")]
                    source = next((p for p in candidates if p.is_file() and p.stat().st_size), None)
                    if source is None:
                        raise AppError("Neolink hat keine verarbeitbare Videodatei erzeugt.")
                    job["state"] = "converting"
                    info = json.loads(await run([self.settings.ffprobe, "-v", "error",
                                                 "-show_streams", "-of", "json", source], 30))
                    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
                    if not video:
                        raise AppError("The recording does not contain a readable video stream.")
                    codec_args = ["-c:v", "copy"] if (
                        video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p"
                    ) else ["-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
                            "-pix_fmt", "yuv420p"]
                    await run([self.settings.ffmpeg, "-v", "error", "-y", "-i", source,
                               "-map", "0:v:0", "-map", "0:a?", *codec_args, "-c:a", "aac",
                               "-movflags", "+faststart", temporary], self.settings.download_seconds)
                    if not temporary.exists() or temporary.stat().st_size == 0:
                        raise AppError("Video processing did not produce a file.")
                    # Decode one frame to catch unusable outputs before declaring ready.
                    await run([self.settings.ffmpeg, "-v", "error", "-i", temporary,
                               "-frames:v", "1", "-f", "null", "-"], 30)
                    temporary.replace(final)
                final.touch()
                job["state"] = "ready"
                self.prune_cache(final)
        except asyncio.CancelledError:
            job["state"], job["error"] = "error", "Wiedergabe wurde beendet."
            raise
        except Exception as exc:
            job["state"] = "error"
            job["error"] = exc.message if isinstance(exc, AppError) else "The recording could not be prepared."
            log.warning("Recording preparation failed (%s)", type(exc).__name__)
        finally:
            job["updated"] = time.monotonic()
            temporary.unlink(missing_ok=True)
            shutil.rmtree(directory, ignore_errors=True)

    def prune_cache(self, protected=None):
        files = sorted(self.cache.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total <= self.settings.cache_bytes:
                break
            if path == protected or path.name.endswith(".part.mp4") or time.time() - path.stat().st_mtime < 600:
                continue
            total -= path.stat().st_size
            path.unlink(missing_ok=True)

    async def expire_leases(self, now=None):
        now = time.monotonic() if now is None else now
        for live in list(self.live.values()):
            for lease, stamp in list(live.leases.items()):
                if now - stamp > self.settings.lease_seconds:
                    await self.stop_live(lease)

    async def reap(self):
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            await self.expire_leases(now)
            for live in list(self.live.values()):
                if live.state == "ready" and live.ffmpeg and live.ffmpeg.returncode is not None:
                    live.state, live.error = "error", "The camera stream was interrupted."
                    await terminate(live.rtsp)
            for key, job in list(self.jobs.items()):
                if job["state"] in ("ready", "error") and now - job["updated"] > 3600:
                    self.jobs.pop(key, None)
            self.prune_cache()

    async def close(self):
        self.closing = True
        if self.reaper:
            self.reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reaper
        tasks = [j["task"] for j in self.jobs.values() if not j["task"].done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for live in list(self.live.values()):
            async with self.locks[live.camera_id]:
                await self.stop_locked(live)
