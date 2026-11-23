# Neolink Studio

[![Docker GHCR](https://img.shields.io/badge/Docker-GHCR-blue?logo=docker)](https://github.com/mhhoeper/neolink-sample-app/pkgs/container/neolink-sample-app)
[![Docker GHCR workflow](https://github.com/mhhoeper/neolink-sample-app/actions/workflows/docker-ghcr.yml/badge.svg)](https://github.com/mhhoeper/neolink-sample-app/actions/workflows/docker-ghcr.yml)

A Docker web application for the cameras in your existing Neolink configuration: browse SD-card recordings on a shared timeline, replay events, download recordings and start live view on demand.

## Install with Docker Compose

Requirements: **Linux, Docker with the Compose plugin, linux/amd64**, access to the camera LAN and a working Neolink configuration. GitHub Actions builds the complete application with Neolink, Python and FFmpeg. The server downloads this image; it does not need Python, application source code or a local Docker build. The base image is currently `ghcr.io/mhhoeper/neolink:dev`; other architectures are not built.

From your own computer, for example in PowerShell:

~~~powershell
ssh SERVER_USER@SERVER_HOST
~~~

Replace `SERVER_USER` and `SERVER_HOST` with your SSH user and server. Run the following Bash commands **inside this Linux SSH session on the Docker server**. Check that Compose is available:

~~~bash
docker compose version
~~~

If the command is unavailable, install the [Docker Compose plugin](https://docs.docker.com/compose/install/linux/) first. On Debian/Ubuntu with Docker's official package repository configured, use `sudo apt-get update` followed by `sudo apt-get install docker-compose-plugin`.

### Download the deployment files

Docker downloads the image, not your configuration or `compose.yaml`. Download these small deployment files once; no Git checkout or start script is needed:

~~~bash
mkdir -p ~/neolink-sample-app/config
cd ~/neolink-sample-app
curl -fSLo compose.yaml https://raw.githubusercontent.com/mhhoeper/neolink-sample-app/main/compose.yaml
curl -fSLo .env.example https://raw.githubusercontent.com/mhhoeper/neolink-sample-app/main/.env.example
cp -n .env.example .env
chmod 600 .env
~~~

`cp -n` preserves an existing `.env`. Set both `WEB_USERNAME` and `WEB_PASSWORD` there to enable HTTP Basic authentication. Leave both empty for access without a login. Use plain `KEY=value` lines without shell quotes or inline comments. `APP_IMAGE` selects the application image; its default is `ghcr.io/mhhoeper/neolink-sample-app:latest`.

### Bind your camera configuration and existing caches

For a **new installation**, replace `/path/to/neolink.toml` with your working configuration on the server:

~~~bash
chmod 700 config
cp -n /path/to/neolink.toml config/neolink.toml
sudo chown "$(id -u):10001" config/neolink.toml
chmod 640 config/neolink.toml
~~~

The [example configuration](config/neolink.example.toml) shows the structure. UID/GID 10001 inside the container must be able to read the file. Camera credentials stay on the server; Compose mounts the file read-only at `/config/neolink.toml`.

For an **existing installation**, keep its configuration and `.env`. Before removing the old container, inspect its mounts:

~~~bash
docker inspect neolink-studio --format '{{range .Mounts}}{{printf "%s: %s -> %s\n" .Type .Source .Destination}}{{end}}'
docker inspect neolink-studio --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Type}} {{.Name}} {{.Source}}{{end}}{{end}}'
~~~

The mount targeting `/config/neolink.toml` gives the existing configuration path. The mount targeting `/data` gives the cache source. Adjust these two entries in `compose.yaml` if necessary:

| Existing data | Compose setting | Container path |
| --- | --- | --- |
| Camera configuration file | `services.studio.volumes`: bind `source`, default `./config/neolink.toml` | `/config/neolink.toml` |
| Named cache volume | `volumes.studio-data.name`, default `neolink-studio-data` | `/data` |

The previous Docker CLI installation uses **`neolink-studio-data`**. Compose explicitly reuses that name, so the existing videos and event lists remain available without copying any cache files. A volume created by an older Compose definition may have another name, such as `neolink-sample-app_studio-data`; use the name reported by `docker inspect`. Validate that the existing volume is present with `docker volume inspect VOLUME_NAME`.

Compose creates a volume only if the configured name does not already exist. **Reusing an existing name does not empty it.** A different name produces a separate, initially empty cache. Neither downloading an image nor creating a volume binds it to a container: the `volumes` entries in `compose.yaml` perform the bindings when Compose starts the service. Volumes remain local to the same Docker server.

If the existing cache is a regular server directory (`Type` is `bind`), replace the `studio-data:/data` service mount with:

~~~yaml
      - type: bind
        source: /ABSOLUTE/CACHE/DIRECTORY
        target: /data
~~~

Use the complete existing data directory containing `recordings/` and `events/`, not either subdirectory alone. UID/GID 10001 needs write access to this directory. An unused top-level `studio-data` declaration may be removed in this case.

### Start the application

For a new installation or an existing service already managed by this Compose project:

~~~bash
cd ~/neolink-sample-app
docker compose config --quiet
docker compose pull
docker compose up -d
~~~

If migrating from a container started without this Compose definition, use the following sequence instead. Download and validate first, then remove only the old application container:

~~~bash
cd ~/neolink-sample-app
docker compose config --quiet
docker compose pull
docker stop -t 20 neolink-studio
docker rm neolink-studio
docker compose up -d
~~~

Keep its named data volume and configuration file. This one-time transition is unnecessary when the container is already managed by the current Compose project.

Open **`http://SERVER_HOST:8080`** from another computer on the reachable network. TCP port 8080 must be accessible. In a browser, `localhost` refers to the computer running that browser. The container continues running after SSH disconnects and uses `restart: unless-stopped`.

Production uses Linux host networking for local UID/UDP camera discovery. Neolink and FFmpeg run in the same container; RTSP listens only on `127.0.0.1` at a dynamic port. The browser needs only the web port. Avoid simultaneous connections from another Neolink instance, especially with battery cameras.

For internet access, use an HTTPS reverse proxy with access control. HTTP Basic alone does not encrypt traffic. The application currently uses absolute `/api` and `/static` paths; hosting it below a URL subpath requires additional changes.

### Update and manage

For every image update, connect by SSH and run:

~~~bash
cd ~/neolink-sample-app
docker compose pull
docker compose up -d
~~~

`pull` downloads the image; `up -d` replaces the container when its image or settings changed and reuses the configured file and volume. Existing recordings under `/data/recordings` and event lists under `/data/events` remain available. Active playback is interrupted during replacement. A container restart alone continues using its previous image.

For a particular application commit, set `APP_IMAGE` in `.env` to a `sha-<full-commit>` tag. For an immutable image, use `ghcr.io/mhhoeper/neolink-sample-app@sha256:<digest>` instead. A commit tag identifies application source; rebuilding it can produce another image because upstream images and system packages can change.

~~~bash
docker compose ps
docker compose logs -f
curl -fsS http://127.0.0.1:8080/healthz
# After changing the contents of the mounted camera configuration:
docker compose restart studio
# Stop and start the existing service:
docker compose stop studio
docker compose start studio
# Inspect the installed commit and matching source code:
docker inspect neolink-studio --format '{{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.url"}}'
~~~

Keep the data volume: **`docker compose down -v` deletes the cache volume.** Run only one application container per data volume; live processes and viewer sessions are managed in memory.

## GitHub Actions and GHCR

[.github/workflows/docker-ghcr.yml](.github/workflows/docker-ghcr.yml) runs application checks, builds a linux/amd64 image and smoke-tests it with demo cameras before publishing.

| Trigger | GHCR tags |
| --- | --- |
| Push to `main` | `latest` and `sha-<full-commit>` |
| Version tag push, for example `v1.0.0` | `v1.0.0` and `sha-<full-commit>` |
| Pull request | Checks and image build; no publication |
| Actions → Docker GHCR → Run workflow | Publishes only for `main` or a `v*` tag; other branches only run checks |

`latest` is the most recent successfully checked `main` build. Version tags do not change `latest`. **GitHub Releases are not created.** OCI labels and manifest/index annotations contain the commit, matching source link and `AGPL-3.0-or-later`. The Actions summary includes the published digest and source link. The smoke-test and publication builds use the same Neolink base image pinned by digest within each run.

For the first publication, commit all application files and the workflow, then push to `main`. Enable Actions and allow the actions used by the workflow. The automatically provided `GITHUB_TOKEN` needs `contents: read` and `packages: write`; no separate token secret is required. The source label links the new GHCR package to the repository. If the package already exists, grant the repository write access to it.

### Make the GHCR package public

For image downloads without a registry login:

1. Let the GitHub Action successfully publish the first image; the package must exist first.
2. Open the [package page](https://github.com/mhhoeper/neolink-sample-app/pkgs/container/neolink-sample-app), also linked by the GHCR badge.
3. Select **Package settings**.
4. Under **Danger Zone → Change visibility**, choose **Public**.
5. Confirm the change and enter the package name if requested.

Package administrator access is required. A public repository does not automatically make its GHCR package public. This setting applies to subsequent versions of the same package as well.

For a private application package, the server needs `docker login ghcr.io` with a suitable token granting `read:packages`. Do not put tokens in documentation or `.env`. The Neolink base image must also be accessible to the build workflow.

The GHCR badge opens the package page; the Actions badge opens the pipeline. These pages become available after the GitHub upload and first publication.

## Browse recordings and live view

The player and shared timeline form one view. By default, recordings from all configured cameras appear together, with one timeline row per camera.

1. **24 hours** shows the rolling interval from the same local time yesterday until now, loading both days. Opening, refreshing or returning to this view updates its end time. **Automatic** uses the main stream for cameras configured with `stream = "main"`, otherwise the substream.
2. Click a recording on the timeline or in the shared list. The player switches to that camera, loads the full recording and starts playback. The timeline zooms to the selected interval.
3. Click a point on the zoomed-in recording bar to seek there. Selecting a list item starts at the recording beginning. Seeking in the player also moves the timeline marker.
4. Choose **24 hours**, **6 hours**, **1 hour** or **10 minutes** to change the overview. Date labels distinguish both sides of midnight. Nearby or overlapping recordings open a selection; each remains individually playable.
5. **Download recording** saves the full prepared MP4 file. Its filename includes camera, recording start and an identifier. Download remains available while paused or after playback ends; it is hidden during live view and preparation.

The date picker selects a single day marked **Archive**. **Now** returns to the last 24 hours. Smaller zoom ranges can move within the loaded interval. Camera buttons filter the overview; selecting a recording can switch the player camera while keeping other camera rows visible.

Bars show the **recording intervals** reported by Neolink, not a precise alarm-trigger timestamp. Recordings without readable timestamps remain playable in the list. Camera timestamps are treated as local wall-clock values without timezone conversion; the browser should use the cameras' local timezone. Timeline dates use `YYYY-MM-DD` and 24-hour time.

At most two cameras are queried concurrently. If a camera is unavailable, other events remain visible. An existing list can be shown as an older cache snapshot, with timestamp and warning; without a cache, the camera is marked unavailable.

**Start live view** connects the selected camera. **Stop**, or pausing the live player, ends that viewer's live session. Viewers share one RTSP process per camera; Neolink leaves RTSP mode after the last viewer stops. Heartbeats expire disconnected viewers after 45 seconds by default, checked every 5 seconds. Closing the page also sends a stop request; throttled background tabs can expire.

During live view, the overview uses cached event lists for that camera and labels their age. Missing lists are reported as incomplete. Stop live view to query again. Cancelling recording preparation closes the view, but an already started download can finish in the background and populate the cache; that camera may remain busy meanwhile.

## Media processing and caches

The application creates a private runtime profile for the selected camera and runs:

~~~bash
neolink --config PRIVATE_CONFIG rtsp
~~~

FFmpeg reads `rtsp://127.0.0.1:PORT/CAMERA/STREAM` and produces HLS. The original configuration remains unchanged. Camera login, UID, address and discovery settings are retained; global MQTT, RTSP users and TLS settings are omitted from the runtime profile. Motion-based pauses are disabled for active live viewing. `stream = "none"` disables live view.

`LIVE_VIDEO_MODE=auto` copies H.264 with `yuv420p` without video re-encoding; H.265 and other formats are converted to H.264. Audio is converted to AAC. `LIVE_VIDEO_MODE=transcode` forces H.264 with regular keyframes, at most 1280 pixels wide and 15 fps. This uses more CPU but produces regular short HLS segments. With H.264 passthrough, camera keyframes determine segment length. Expect several seconds of latency, potentially more with long keyframe intervals. The substream is preferred; `stream = "main"` or `"extern"` is respected.

Recordings are obtained through:

~~~bash
neolink --config PRIVATE_CONFIG replay CAMERA files --date YYYY-MM-DD --stream subStream
neolink --config PRIVATE_CONFIG replay CAMERA download --name FILENAME --stream subStream --output download.mp4
~~~

CLI output is validated. Failures are shown as errors rather than empty lists. Neolink must support the camera's recording functions; an SD card with recordings is required. AI filters depend on camera metadata.

FFmpeg prepares browser-compatible MP4 files, preferably remuxing H.264 and converting H.265. `.h264`/`.h265` fallback files are supported; proprietary raw formats are not. Downloads have no artificial short `--duration` limit. Long or stalled operations eventually time out with an error.

The recording cache has a **soft limit of 2 GiB**. Older recordings are removed first, while recently used files are protected for ten minutes. Active downloads and conversions can temporarily exceed the limit. The camera's SD card is unchanged.

Event lists persist for reuse. They are fresh for 60 seconds by default (`EVENT_CACHE_SECONDS`); after that, the next view queries again. Refresh forces a new query. Older cached lists may be used during live view and are labelled accordingly. Up to 1,000 lists are retained, then the oldest are removed.

The default Compose volume is **`neolink-studio-data`**, mounted at `/data`. Use the existing name if migrating a differently named volume.

| Contents | Container path |
| --- | --- |
| Prepared MP4 recordings | `/data/recordings` |
| Event lists as JSON | `/data/events` |
| Temporary downloads, HLS and runtime profiles | `/data/runtime` |

Find the host location with `docker volume inspect neolink-studio-data --format '{{.Mountpoint}}'`, replacing the name if needed. The path depends on the Docker installation. Persistent recordings and lists survive container replacement; temporary runtime files are cleaned after use and recreated at startup. No camera media is cached in the Windows repository. A recording downloaded in the browser is additionally saved to that device's selected download folder.

## iPhone notifications

The application does not yet send push notifications or continuously monitor cameras for new events. Event lists are loaded on request; granting iPhone permissions alone does not enable notifications.

Web Push requires iOS 16.4 or later, trusted HTTPS, installation through Safari → Share → **Add to Home Screen**, and an explicit action to allow notifications. Plain HTTP is insufficient. Implementation would also require a web app manifest, service worker, push subscriptions and a server-side event monitor. Apple delivers notifications over the internet; accessing the camera view from outside the LAN may additionally require a VPN.

An event monitor would need stable event identifiers and deduplication. Frequent `replay files` polling detects recordings only after they exist and may wake battery cameras or compete with live view and downloads. Any direct event source must be checked for its effect on camera sleep; persistent local connections may drain batteries. For immediate notifications, use the Reolink application's push function independently of Neolink Studio.

References: [WebKit: Web Push for iOS Home Screen web apps](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/), [MDN: Push API](https://developer.mozilla.org/en-US/docs/Web/API/Push_API).

## Demo without cameras

In a local repository with Docker Compose and an available port 8080:

~~~bash
docker compose -f compose.demo.yaml pull
docker compose -f compose.demo.yaml up
~~~

**http://localhost:8080** displays clearly marked sample data and FFmpeg test video. This exercises the interface, playback and session lifecycle, not a real camera connection.

## Local development and validation

Python 3.11+, FFmpeg and FFprobe are required; real camera operation also needs Neolink with `replay` and `rtsp`.

~~~bash
python -m venv .venv
# Linux:
. .venv/bin/activate
# Windows alternative: .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest -q
node tests/timeline.test.cjs
~~~

Local demo on Linux:

~~~bash
DEMO_MODE=true APP_DATA=./data uvicorn app.main:app --port 8080
~~~

Local demo in PowerShell:

~~~powershell
$env:DEMO_MODE = "true"
$env:APP_DATA = "./data"
uvicorn app.main:app --port 8080
~~~

For real local operation, set `NEOLINK_CONFIG` to an existing TOML file. Use **one Uvicorn worker and one container per data volume**.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LIVE_LEASE_SECONDS` | `45` | Expiration after the last heartbeat |
| `LIVE_STARTUP_SECONDS` | `90` | Maximum live startup time |
| `QUERY_TIMEOUT_SECONDS` | `60` | Event query timeout |
| `DOWNLOAD_TIMEOUT_SECONDS` | `900` | Download/conversion timeout |
| `EVENT_CACHE_SECONDS` | `60` | Freshness of cached event lists |
| `CACHE_MAX_GB` | `2` | Soft recording-cache limit in GiB |
| `NEOLINK_BIN` / `FFMPEG_BIN` / `FFPROBE_BIN` | Program name | Alternative executable paths |

For a real-camera system check, compare events with the Neolink CLI, replay a full recording, then start, pause and stop live view. Use `docker compose top` to confirm Neolink/FFmpeg processes end. With two browsers, verify stopping one viewer keeps the other active. After a browser disconnects, check again after lease expiry plus the reaper interval.

## License and source code

This application is **AGPL-3.0-or-later**; see [LICENSE](LICENSE). [Neolink](https://github.com/mhhoeper/neolink) uses the same license. The interface includes an application source-code link; image metadata provides its matching commit and source URL.

HLS.js is bundled locally; its version and Apache-2.0 license are in `app/static/vendor/`. Python packages and FFmpeg retain their respective licenses. When distributing custom images, provide matching source for the application and the Neolink image. `APP_REVISION` and `APP_SOURCE_URL` set image metadata at build time; metadata does not replace a source-code offer for modified versions.
