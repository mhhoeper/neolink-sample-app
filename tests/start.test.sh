#!/usr/bin/env bash
# Test container replacement with a fake Docker CLI; never contact a daemon.
set -euo pipefail
project="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
temp_root="${TMPDIR:-/tmp}"
fixture="$(mktemp -d "$temp_root/neolink-start-tests.XXXXXX")"
# Only recursively clean our own mktemp directory under the selected temp root.
case "$fixture" in "$temp_root"/neolink-start-tests.*) ;; *) exit 2 ;; esac
trap 'rm -rf -- "$fixture"' EXIT
mkdir -p "$fixture/bin" "$fixture/project/scripts" "$fixture/project/config"
cp "$project/scripts/start.sh" "$fixture/project/scripts/start.sh"
cp "$project/.env.example" "$fixture/project/.env.example"
printf '[[cameras]]\nname = "Test"\n' > "$fixture/project/config/neolink.toml"
export DOCKER_TEST_LOG="$fixture/docker.log"
cat > "$fixture/bin/docker" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$DOCKER_TEST_LOG"
source="https://github.com/mhhoeper/neolink-sample-app"
case "$1" in
  info) ;;
  pull) [[ "$DOCKER_TEST_SCENARIO" != pull-failure ]] ;;
  image)
    if [[ "$DOCKER_TEST_SCENARIO" == foreign-image ]]; then
      echo 'https://example.com/other-app'
    else
      echo "$source"
    fi
    ;;
  container)
    if [[ "$*" == *org.opencontainers.image.source* ]]; then
      if [[ "$DOCKER_TEST_SCENARIO" == foreign-container ]]; then
        echo 'https://example.com/other-app'
      else
        echo "$source"
      fi
    elif [[ "$*" == *.State.Running* ]]; then
      [[ "$DOCKER_TEST_SCENARIO" != exited ]] && echo true || echo false
    elif [[ "$*" == *.State.Health.Status* ]]; then
      [[ "$DOCKER_TEST_SCENARIO" != unhealthy ]] && echo healthy || echo unhealthy
    else
      [[ "$DOCKER_TEST_SCENARIO" != fresh ]]
    fi
    ;;
  run)
    if [[ "$2" == --rm ]]; then
      [[ "$DOCKER_TEST_SCENARIO" != invalid-config ]]
    else
      echo test-container
    fi
    ;;
  stop|rm|volume|logs) ;;
  *) echo "Unexpected Docker command: $*" >&2; exit 2 ;;
esac
MOCK
# Ensure a developer's real port 8080 and health-check sleeps cannot affect tests.
printf '#!/usr/bin/env bash\nexit 0\n' > "$fixture/bin/ss"
printf '#!/usr/bin/env bash\nexit 0\n' > "$fixture/bin/sleep"
chmod +x "$fixture/bin/"*
export PATH="$fixture/bin:$PATH"
count=0
run_case() {
  export DOCKER_TEST_SCENARIO="$1"
  local expected="$2"
  shift 2
  : > "$DOCKER_TEST_LOG"
  local status=0
  bash "$fixture/project/scripts/start.sh" "$@" > "$fixture/output" 2>&1 || status="$?"
  if [[ "$status" != "$expected" ]]; then
    cat "$fixture/output" >&2
    echo "Scenario $DOCKER_TEST_SCENARIO: exit $status, expected $expected" >&2
    exit 1
  fi
  count=$((count + 1))
}
no_replacement() {
  if grep -Eq '^(stop |rm |run -d |volume )' "$DOCKER_TEST_LOG"; then
    echo "Preflight failure replaced a container or touched its volume" >&2
    cat "$DOCKER_TEST_LOG" >&2
    exit 1
  fi
}
run_case success 0
grep -q '^pull ghcr.io/mhhoeper/neolink-sample-app:latest$' "$DOCKER_TEST_LOG"
grep -q '^stop -t 20 neolink-studio$' "$DOCKER_TEST_LOG"
grep -q '^volume create neolink-studio-data$' "$DOCKER_TEST_LOG"
grep -q 'type=volume,source=neolink-studio-data,target=/data' "$DOCKER_TEST_LOG"
[[ $(grep -n '^run --rm' "$DOCKER_TEST_LOG" | cut -d: -f1) -lt $(grep -n '^stop ' "$DOCKER_TEST_LOG" | cut -d: -f1) ]]
! grep -Eq '^build |^volume rm ' "$DOCKER_TEST_LOG"
run_case fresh 0
! grep -Eq '^stop |^rm ' "$DOCKER_TEST_LOG"
run_case pull-failure 1
no_replacement
run_case foreign-image 1
no_replacement
run_case invalid-config 1
no_replacement
run_case foreign-container 1
no_replacement
mv "$fixture/project/config/neolink.toml" "$fixture/project/config/saved.toml"
run_case success 1
no_replacement
mv "$fixture/project/config/saved.toml" "$fixture/project/config/neolink.toml"
run_case success 0 --pull-only
no_replacement
! grep -q '^run ' "$DOCKER_TEST_LOG"
# Existing installations' old build variables must not select the runtime image.
printf 'NEOLINK_IMAGE=old-base\nAPP_REVISION=old-commit\nAPP_IMAGE=ghcr.io/mhhoeper/neolink-sample-app:sha-test\r\n' > "$fixture/project/.env"
run_case success 0 --skip-pull
! grep -q '^pull ' "$DOCKER_TEST_LOG"
grep -q 'ghcr.io/mhhoeper/neolink-sample-app:sha-test' "$DOCKER_TEST_LOG"
run_case unhealthy 1
grep -q '^logs --tail 40 neolink-studio$' "$DOCKER_TEST_LOG"
run_case exited 1
grep -q '^logs --tail 40 neolink-studio$' "$DOCKER_TEST_LOG"
run_case success 2 --unknown
no_replacement
run_case success 2 --skip-pull unexpected
no_replacement
printf '%s deployment scenarios passed\n' "$count"
