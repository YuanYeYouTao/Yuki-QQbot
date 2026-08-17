#!/bin/sh
set -eu

VERSION="3.5.3"
INSTALL_DIR=""
REPOSITORY="YuanYeYouTao/Yuki-QQbot"
BOT_IMAGE="ghcr.io/yuanyeyoutao/yuki-qqbot"

usage() {
    printf '%s\n' "Usage: install.sh [--dir PATH] [--version X.Y.Z]"
}

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dir)
            [ "$#" -ge 2 ] || fail "--dir requires a path"
            INSTALL_DIR=$2
            shift 2
            ;;
        --version)
            [ "$#" -ge 2 ] || fail "--version requires X.Y.Z"
            VERSION=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

printf '%s\n' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || \
    fail "version must use X.Y.Z"

command -v docker >/dev/null 2>&1 || fail "Docker is not installed"
docker compose version >/dev/null 2>&1 || fail "the Docker Compose CLI plugin is not available"
docker info >/dev/null 2>&1 || fail "Docker Engine is not running"

architecture=$(docker info --format '{{.Architecture}}' 2>/dev/null || true)
operating_system=$(docker info --format '{{.OSType}}' 2>/dev/null || true)
[ "$operating_system" = linux ] || fail "Docker must be running Linux containers"
case "$architecture" in
    amd64|x86_64) ;;
    *) fail "Yuki $VERSION officially supports linux/amd64; Docker reports $architecture" ;;
esac

if [ -z "$INSTALL_DIR" ]; then
    if [ -f "docker-compose.yml" ] && [ -f ".env.example" ]; then
        INSTALL_DIR=$PWD
    else
        INSTALL_DIR=$PWD/yuki
    fi
fi
mkdir -p "$INSTALL_DIR"
INSTALL_DIR=$(cd "$INSTALL_DIR" && pwd)
[ -w "$INSTALL_DIR" ] || fail "installation directory is not writable"

existing=false
if [ -f "$INSTALL_DIR/docker-compose.yml" ] && [ -f "$INSTALL_DIR/.env.example" ]; then
    existing=true
elif [ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    fail "installation directory is not empty and is not a Yuki deployment"
fi

temporary=$(mktemp -d "${TMPDIR:-/tmp}/yuki-install.XXXXXX")
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

download() {
    url=$1
    output=$2
    if command -v curl >/dev/null 2>&1; then
        curl --fail --silent --show-error --location "$url" --output "$output"
    elif command -v wget >/dev/null 2>&1; then
        wget -q "$url" -O "$output"
    else
        fail "curl or wget is required to download the release"
    fi
}

base="https://github.com/$REPOSITORY/releases/download/v$VERSION"
archive="yuki-$VERSION-deploy.tar.gz"
download "$base/$archive" "$temporary/$archive"
download "$base/SHA256SUMS" "$temporary/SHA256SUMS"
expected=$(awk -v name="$archive" '$2 == name {print $1}' "$temporary/SHA256SUMS")
[ -n "$expected" ] || fail "release checksum does not list $archive"
if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$temporary/$archive" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    actual=$(shasum -a 256 "$temporary/$archive" | awk '{print $1}')
else
    fail "sha256sum or shasum is required"
fi
[ "$actual" = "$expected" ] || fail "release archive checksum mismatch"
tar -xzf "$temporary/$archive" -C "$temporary"
source="$temporary/yuki-$VERSION-deploy"
[ -d "$source" ] || fail "release archive layout is invalid"

if [ "$existing" = false ]; then
    cp -R "$source/." "$INSTALL_DIR/"
else
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    managed_backup="$INSTALL_DIR/.yuki/backups/installer-$stamp"
    for relative in docker-compose.yml .env.example install.sh install.ps1 "Yuki-$VERSION-Upgrade.md"; do
        [ -f "$source/$relative" ] || fail "release bundle is missing $relative"
        if [ -f "$INSTALL_DIR/$relative" ]; then
            mkdir -p "$managed_backup/$(dirname "$relative")"
            cp "$INSTALL_DIR/$relative" "$managed_backup/$relative"
        fi
        cp "$source/$relative" "$INSTALL_DIR/$relative.yuki-new"
        mv -f "$INSTALL_DIR/$relative.yuki-new" "$INSTALL_DIR/$relative"
    done
    printf '%s\n' "Updated release-managed deployment files; mutable data and configuration were preserved."
fi

if [ "$existing" = false ] && command -v ss >/dev/null 2>&1 && \
    ss -ltn 2>/dev/null | grep -Eq '[:.]6099[[:space:]]'; then
    fail "TCP port 6099 is already in use"
fi

image="$BOT_IMAGE:$VERSION"
printf '%s\n' "Pulling $image"
docker pull "$image"

if [ "$existing" = true ]; then
    cd "$INSTALL_DIR"
    old_running=$(docker compose ps -q bot 2>/dev/null || true)
    if [ -n "$old_running" ]; then
        docker compose stop bot || fail "unable to stop the old Bot container"
        still_running=$(docker inspect --format '{{.State.Running}}' "$old_running" 2>/dev/null || printf '%s\n' false)
        [ "$still_running" = "false" ] || fail "old Bot container is still writing the database"
    fi
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    snap="$INSTALL_DIR/.yuki/backups/upgrade-3.6/$stamp"
    mkdir -p "$snap/data" "$snap/config" || fail "unable to create the 3.6.0 upgrade snapshot directory"
    copy_upgrade_file() {
        source=$1
        target=$2
        if [ -f "$source" ]; then
            mkdir -p "$(dirname "$target")"
            cp -p "$source" "$target" || fail "unable to snapshot $source"
        fi
    }
    copy_upgrade_file "$INSTALL_DIR/.env" "$snap/.env"
    copy_upgrade_file "$INSTALL_DIR/.mcp.json" "$snap/.mcp.json"
    copy_upgrade_file "$INSTALL_DIR/config/model_profiles.toml" "$snap/config/model_profiles.toml"
    copy_upgrade_file "$INSTALL_DIR/docker-compose.yml" "$snap/docker-compose.yml"
    copy_upgrade_file "$INSTALL_DIR/data/qq_ai_bot.db" "$snap/data/qq_ai_bot.db"
    copy_upgrade_file "$INSTALL_DIR/data/qq_ai_bot.db-wal" "$snap/data/qq_ai_bot.db-wal"
    copy_upgrade_file "$INSTALL_DIR/data/qq_ai_bot.db-shm" "$snap/data/qq_ai_bot.db-shm"
    digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$image" 2>/dev/null || true)
    printf '%s\n' "{\"version\":\"$VERSION\",\"image\":\"$image\",\"digest\":\"$digest\",\"created_at\":\"$stamp\"}" > "$snap/manifest.json" \
        || fail "unable to write the upgrade snapshot manifest"
    verify_snapshot='
import hashlib, pathlib, sqlite3
root = pathlib.Path("/snapshot")
rows = []
for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rows.append(f"{digest}  {path.relative_to(root).as_posix()}")
(root / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="utf-8")
for line in rows:
    expected, relative = line.split("  ", 1)
    actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit(f"checksum mismatch: {relative}")
db = root / "data/qq_ai_bot.db"
if db.is_file():
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    status = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()
    if status != "ok":
        raise SystemExit("sqlite integrity_check failed")
'
    docker run --rm \
        --user "$(id -u):$(id -g)" \
        --entrypoint python \
        --volume "$snap:/snapshot" \
        "$image" -c "$verify_snapshot" \
        || fail "upgrade snapshot checksum or database verification failed"
    docker run --rm \
        --user "$(id -u):$(id -g)" \
        --entrypoint qq-ai-bot-cli \
        --volume "$INSTALL_DIR:/deploy" \
        --workdir /deploy \
        "$image" setup migrate-3-6 --deployment-root /deploy --no-color \
        --baseline-output "/deploy/.yuki/backups/upgrade-3.6/$stamp/baseline-v1.json" \
        || fail "setup migrate-3-6 failed"
fi

[ -t 0 ] && [ -t 1 ] || fail "guided setup requires an interactive terminal"
docker run --rm -it \
    --user "$(id -u):$(id -g)" \
    --entrypoint qq-ai-bot-cli \
    --volume "$INSTALL_DIR:/deploy" \
    --workdir /deploy \
    "$image" setup --deployment-root /deploy

cd "$INSTALL_DIR"
docker compose config --quiet
docker compose pull
old_bot=$(docker compose ps -q bot 2>/dev/null || true)
docker compose up -d

wait_for_bot() {
    deadline=$(( $(date +%s) + 180 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        container=$(docker compose ps -q bot 2>/dev/null || true)
        if [ -n "$container" ]; then
            status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)
            [ "$status" = healthy ] && return 0
            [ "$status" = exited ] && return 1
        fi
        sleep 2
    done
    return 1
}

wait_for_service() {
    service=$1
    deadline=$(( $(date +%s) + 180 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        container=$(docker compose --profile speech ps -q "$service" 2>/dev/null || true)
        if [ -n "$container" ]; then
            status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)
            [ "$status" = healthy ] && return 0
            [ "$status" = exited ] && return 1
        fi
        sleep 2
    done
    return 1
}

wait_for_bot || fail "Bot did not become healthy within 180 seconds"
new_bot=$(docker compose ps -q bot 2>/dev/null || true)
if [ -f "data/setup/restart-required" ] && [ "$old_bot" != "$new_bot" ]; then
    rm -f "data/setup/restart-required"
fi
if [ -f "data/setup/speech-action" ]; then
    speech_action=$(tr -d '\r\n' < "data/setup/speech-action")
    case "$speech_action" in
        start)
            docker compose --profile speech up -d --no-deps genie-tts-worker
            wait_for_service genie-tts-worker || fail "Speech Worker did not become healthy"
            ;;
        stop)
            docker compose --profile speech stop genie-tts-worker
            docker compose --profile speech rm -f genie-tts-worker
            ;;
        *) fail "unknown pending Speech action" ;;
    esac
    rm -f "data/setup/speech-action"
fi
if [ -f "data/setup/pending.json" ]; then
    docker compose exec -T bot qq-ai-bot-cli setup apply-pending --deployment-root /app --no-color
fi
if [ -f "data/setup/restart-required" ]; then
    docker compose restart bot
    wait_for_bot || fail "Bot did not become healthy after applying configuration"
    rm -f "data/setup/restart-required"
fi
docker compose exec -T bot qq-ai-bot-cli setup verify --deployment-root /app --no-color
