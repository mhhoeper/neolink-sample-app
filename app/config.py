"""Read the existing Neolink TOML without disclosing camera credentials."""
import copy
import hashlib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w


@dataclass
class Settings:
    config: Path = Path(os.getenv("NEOLINK_CONFIG", "/config/neolink.toml"))
    data: Path = Path(os.getenv("APP_DATA", "/data"))
    neolink: str = os.getenv("NEOLINK_BIN", "neolink")
    ffmpeg: str = os.getenv("FFMPEG_BIN", "ffmpeg")
    ffprobe: str = os.getenv("FFPROBE_BIN", "ffprobe")
    demo: bool = os.getenv("DEMO_MODE", "false").lower() == "true"
    lease_seconds: float = float(os.getenv("LIVE_LEASE_SECONDS", "45"))
    startup_seconds: float = float(os.getenv("LIVE_STARTUP_SECONDS", "90"))
    query_seconds: float = float(os.getenv("QUERY_TIMEOUT_SECONDS", "60"))
    download_seconds: float = float(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "900"))
    event_cache_seconds: float = float(os.getenv("EVENT_CACHE_SECONDS", "60"))
    cache_bytes: int = int(float(os.getenv("CACHE_MAX_GB", "2")) * 1024**3)
    username: str = os.getenv("WEB_USERNAME", "")
    password: str = os.getenv("WEB_PASSWORD", "")


DEMO_CAMERAS = [
    {"name": "Driveway", "username": "demo", "uid": "demo-1"},
    {"name": "Garden", "username": "demo", "uid": "demo-2"},
    {"name": "Front door", "username": "demo", "uid": "demo-3"},
]


class Cameras:
    def __init__(self, settings: Settings):
        self.settings = settings
        if bool(settings.username) != bool(settings.password):
            raise ValueError("Set WEB_USERNAME and WEB_PASSWORD together.")
        self.document = {"cameras": DEMO_CAMERAS} if settings.demo else tomllib.loads(
            settings.config.read_text(encoding="utf-8")
        )
        entries = self.document.get("cameras", [])
        if not entries:
            raise ValueError("The Neolink configuration does not contain any cameras.")
        self.entries = {}
        for camera in entries:
            name = camera.get("name")
            if not isinstance(name, str) or not name or len(name) > 150:
                raise ValueError("Each camera needs a valid name.")
            key = hashlib.sha256(name.encode()).hexdigest()[:16]
            if key in self.entries:
                raise ValueError("Camera names must be unique.")
            self.entries[key] = copy.deepcopy(camera)

    def get(self, camera_id):
        return self.entries[camera_id]

    def stream(self, camera_id):
        value = self.get(camera_id).get("stream", "both").lower()
        if value in ("none",):
            raise ValueError("This camera stream is disabled in Neolink.")
        if value in ("main", "mainstream"):
            return "mainStream"
        if value in ("extern", "externstream"):
            return "externStream"
        return "subStream"

    def public(self):
        return [
            {"id": key, "name": item["name"],
             "live_available": item.get("stream", "both").lower() != "none"}
            for key, item in self.entries.items()
        ]

    def runtime(self, camera_id, directory: Path, port=8554):
        directory.mkdir(parents=True, exist_ok=True)
        camera = copy.deepcopy(self.get(camera_id))
        # Camera login/discovery remain intact. Web streaming uses a private,
        # anonymous loopback RTSP endpoint; global MQTT/TLS/users are unnecessary.
        camera["permitted_users"] = ["anonymous"]
        camera["pause"] = {"on_motion": False, "on_client": True, "mode": "none"}
        camera["use_splash"] = False
        camera.pop("mqtt", None)
        document = {"cameras": [camera], "bind": "127.0.0.1", "bind_port": port}
        path = directory / "neolink.toml"
        path.write_text(tomli_w.dumps(document), encoding="utf-8")
        path.chmod(0o600)
        return path
