"""Single-worker web application. Neolink and FFmpeg stay in this container."""
import asyncio
import base64
import contextlib
import hmac
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .replay import AppError
from .service import Service

STATIC = Path(__file__).parent / "static"


def create_app(settings=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        service = Service(settings)
        app.state.service = service
        await service.open()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(title="Neolink Studio", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.middleware("http")
    async def protection(request: Request, call_next):
        if request.url.path != "/healthz" and settings.username:
            credentials = request.headers.get("authorization", "")
            valid = False
            if credentials.startswith("Basic "):
                try:
                    decoded = base64.b64decode(credentials[6:], validate=True).decode()
                    user, password = decoded.split(":", 1)
                    valid = hmac.compare_digest(user.encode(), settings.username.encode()) and hmac.compare_digest(
                        password.encode(), settings.password.encode()
                    )
                except (ValueError, UnicodeError):
                    pass
            if not valid:
                return JSONResponse({"detail": "Authentication required."}, status_code=401,
                                    headers={"WWW-Authenticate": 'Basic realm="Neolink Studio"'})
        if request.method in ("POST", "DELETE", "PUT", "PATCH"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.url.netloc:
                return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
            "worker-src 'self' blob:; object-src 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/api/cameras")
    async def cameras(request: Request):
        service = request.app.state.service
        return {"cameras": service.cameras.public(), "demo": settings.demo,
                "lease_seconds": settings.lease_seconds}

    @app.get("/api/events")
    async def combined_events(request: Request, day: date | None = None, until: datetime | None = None,
                              stream: str = Query("auto", pattern="^(auto|mainStream|subStream)$"),
                              refresh: bool = False):
        if until is not None:
            if day is not None:
                raise AppError("Specify either a date or the end of the 24-hour window.", 422)
            return await request.app.state.service.list_recent_events(until, stream, refresh)
        if day is None:
            raise AppError("Specify a date or the end of the 24-hour window.", 422)
        result = await request.app.state.service.list_all_events(day.isoformat(), stream, refresh)
        return {**result, "day": day.isoformat()}

    @app.get("/api/cameras/{camera_id}/events")
    async def events(request: Request, camera_id: str, day: date,
                     stream: str = Query("subStream", pattern="^(mainStream|subStream)$"), refresh: bool = False):
        result = await request.app.state.service.list_events(camera_id, day.isoformat(), stream, refresh)
        return {**result, "day": day.isoformat()}

    @app.post("/api/cameras/{camera_id}/live", status_code=202)
    async def start_live(request: Request, camera_id: str):
        try:
            return await request.app.state.service.start_live(camera_id)
        except ValueError as exc:
            raise AppError(str(exc), 409) from exc

    @app.post("/api/live/{lease}/heartbeat")
    async def heartbeat(request: Request, lease: str):
        return await request.app.state.service.heartbeat(lease)

    @app.delete("/api/live/{lease}", status_code=204)
    @app.post("/api/live/{lease}/stop", status_code=204)
    async def stop_live(request: Request, lease: str):
        await request.app.state.service.stop_live(lease)

    @app.get("/api/live/{lease}/{filename}")
    async def live_file(request: Request, lease: str, filename: str):
        live = request.app.state.service.find_live(lease)
        if filename != "index.m3u8" and not re.fullmatch(r"segment-\d{6,12}\.ts", filename):
            raise AppError("File not found.", 404)
        if live.state == "error":
            raise AppError(live.error, 502)
        path = live.directory / filename
        if not path.is_file():
            raise AppError("Live video is being prepared.", 404)
        playlist = filename.endswith(".m3u8")
        return FileResponse(path, media_type="application/vnd.apple.mpegurl" if playlist else "video/mp2t",
                            headers={"Cache-Control": "no-store" if playlist else "private, max-age=20"})

    @app.post("/api/cameras/{camera_id}/events/{event_id}/prepare", status_code=202)
    async def prepare(request: Request, camera_id: str, event_id: str):
        return await request.app.state.service.prepare(camera_id, event_id)

    @app.get("/api/jobs/{job_id}")
    async def job(request: Request, job_id: str):
        return request.app.state.service.public_job(job_id)

    @app.get("/api/recordings/{filename}")
    async def recording(request: Request, filename: str):
        if not re.fullmatch(r"[a-f0-9]{64}\.mp4", filename):
            raise AppError("Recording not found.", 404)
        path = request.app.state.service.cache / filename
        if not path.is_file():
            raise AppError("The recording is no longer cached. Select it again.", 404)
        path.touch()
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/cameras/{camera_id}/events/{event_id}/download")
    async def download_recording(request: Request, camera_id: str, event_id: str):
        if not re.fullmatch(r"[a-f0-9]{64}", event_id):
            raise AppError("Recording not found.", 404)
        service = request.app.state.service
        event = service.find_event(camera_id, event_id)
        path = service.cache / f"{event_id}.mp4"
        if not path.is_file():
            raise AppError("The recording is no longer cached. Select it again.", 404)
        camera = re.sub(r"[^\w-]+", "-", service.cameras.get(camera_id)["name"]).strip("-_")[:80] or "Camera"
        try:
            timestamp = datetime.fromisoformat(event["start"]).strftime("%Y-%m-%d_%H-%M-%S")
        except (ValueError, TypeError, KeyError):
            timestamp = "Recording"
        path.touch()
        return FileResponse(path, media_type="video/mp4",
                            filename=f"{camera}_{timestamp}_{event_id[:8]}.mp4",
                            content_disposition_type="attachment",
                            headers={"Cache-Control": "private, no-store"})

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
