#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail
app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$app_root"
mode="${1:-}"
case "$mode" in
  ""|--pull-only|--skip-pull) ;;
  *) echo "Usage: bash scripts/start.sh [--pull-only|--skip-pull]" >&2; exit 2 ;;
esac
[[ "$#" -le 1 ]] || { echo "Too many arguments." >&2; exit 2; }
command -v docker >/dev/null || { echo "Docker is not installed on this host." >&2; exit 1; }
docker info >/dev/null
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi
# Do not source .env: it is a Docker env-file, not executable shell code.
image="$(sed -n 's/^APP_IMAGE=//p' .env | tail -n 1 | tr -d '\r')"
image="${image:-ghcr.io/mhhoeper/neolink-sample-app:latest}"
source="https://github.com/mhhoeper/neolink-sample-app"
name="neolink-studio"
volume="neolink-studio-data"
if [[ "$mode" != --skip-pull ]]; then
  docker pull "$image"
fi
image_source="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.source"}}' "$image")"
[[ "$image_source" == "$source" ]] || {
  echo "The image belongs to another application; start aborted." >&2
  exit 1
}
[[ "$mode" == --pull-only ]] && exit 0
if [[ ! -f config/neolink.toml ]]; then
  echo "Camera configuration is missing: $app_root/config/neolink.toml" >&2
  echo "Place the existing Neolink configuration there and start again." >&2
  exit 1
fi
# Validate the config and env as the image's normal user before stopping anything.
docker run --rm --env-file "$app_root/.env" --env NEOLINK_CONFIG=/config/neolink.toml --env APP_DATA=/data --entrypoint python \
  --mount "type=bind,source=$app_root/config/neolink.toml,target=/config/neolink.toml,readonly" \
  "$image" -c 'from app.config import Cameras, Settings; Cameras(Settings())' || {
    echo "Configuration is invalid or unreadable by UID 10001; start aborted." >&2
    exit 1
  }
if docker container inspect "$name" >/dev/null 2>&1; then
  source_label="$(docker container inspect --format '{{index .Config.Labels "org.opencontainers.image.source"}}' "$name")"
  [[ "$source_label" == "$source" ]] || {
    echo "Container $name belongs to another application; start aborted." >&2
    exit 1
  }
  docker stop -t 20 "$name"
  docker rm "$name"
elif command -v ss >/dev/null && [[ -n "$(ss -H -ltn 'sport = :8080')" ]]; then
  echo "Port 8080 is already in use; start aborted." >&2
  exit 1
fi
docker volume create "$volume" >/dev/null
docker run -d --name "$name" --restart unless-stopped --stop-timeout 20 \
  --network host --env-file "$app_root/.env" --env NEOLINK_CONFIG=/config/neolink.toml --env APP_DATA=/data \
  --mount "type=bind,source=$app_root/config/neolink.toml,target=/config/neolink.toml,readonly" \
  --mount "type=volume,source=$volume,target=/data" "$image"
for attempt in $(seq 1 40); do
  running="$(docker container inspect --format '{{.State.Running}}' "$name")"
  [[ "$running" == true ]] || break
  status="$(docker container inspect --format '{{.State.Health.Status}}' "$name")"
  if [[ "$status" == healthy ]]; then
    printf 'Neolink Studio is running: http://%s:8080\n' "$(hostname)"
    exit 0
  fi
  [[ "$status" == unhealthy ]] && break
  sleep 2
done
echo "The application is not healthy yet. Recent logs:" >&2
docker logs --tail 40 "$name"
exit 1
