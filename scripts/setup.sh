#!/usr/bin/env bash
# ==============================================================================
# fade-out — First-time setup script
# Creates directories, configures environment, builds and starts containers
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
info "Checking dependencies..."

if ! command -v docker &>/dev/null; then
    error "Docker is not installed. Install Docker first: https://docs.docker.com/get-docker/"
fi

if docker compose version &>/dev/null; then
    COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
else
    error "docker-compose is not installed. Install it: https://docs.docker.com/compose/install/"
fi

info "Using: $COMPOSE"

# ---------------------------------------------------------------------------
# Media directory creation
#
# Each of these is a host path that docker-compose.yml bind-mounts into the
# container. They default to ./media/* next to the compose file; override any
# of them in .env to point at a NAS share instead (see .env.example).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Local data directory
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

info "Creating local data directory..."
mkdir -p data

# Load any host-path overrides the user has already set in .env.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

info "Creating media directories..."
for dir in \
    "${WATCH_AUDIO_DIR:-./media/audio}" \
    "${WATCH_VIDEO_DIR:-./media/video}" \
    "${WATCH_CUE_DIR:-./media/cue-sheets}" \
    "${WATCH_SHORTS_DIR:-./media/shorts}" \
    "${OUTPUT_THUMBNAILS_DIR:-./media/thumbnails}" \
    "${OUTPUT_COVER_ART_DIR:-./media/cover-art}"; do
    if [ -d "$dir" ]; then
        info "  exists:  $dir"
    else
        info "  creating: $dir"
        mkdir -p "$dir"
    fi
done

# ---------------------------------------------------------------------------
# Environment file
# ---------------------------------------------------------------------------
if [ -f .env ]; then
    warn ".env already exists. Skipping environment setup."
    warn "Edit .env manually if you need to change values."
else
    info "Creating .env from template..."
    cp .env.example .env

    echo ""
    echo "========================================"
    echo "  Configure your environment variables  "
    echo "========================================"
    echo ""

    read -rp "OpenAI API Key: " openai_key
    read -rp "fal.ai API Key: " fal_key
    read -rp "SoundCloud Email: " sc_email
    read -rsp "SoundCloud Password: " sc_pass
    echo ""
    read -rp "YouTube Client ID: " yt_client_id
    read -rp "YouTube Client Secret: " yt_client_secret
    read -rp "YouTube Refresh Token: " yt_refresh_token
    read -rp "Discord Webhook URL (optional, press Enter to skip): " discord_webhook

    # Write values (macOS and Linux compatible sed)
    if [[ "$OSTYPE" == "darwin"* ]]; then
        SED_INPLACE="sed -i ''"
    else
        SED_INPLACE="sed -i"
    fi

    $SED_INPLACE "s|OPENAI_API_KEY=sk-...|OPENAI_API_KEY=${openai_key}|" .env
    $SED_INPLACE "s|FAL_API_KEY=fal-...|FAL_API_KEY=${fal_key}|" .env
    $SED_INPLACE "s|SOUNDCLOUD_EMAIL=you@example.com|SOUNDCLOUD_EMAIL=${sc_email}|" .env
    $SED_INPLACE "s|SOUNDCLOUD_PASSWORD=your-password|SOUNDCLOUD_PASSWORD=${sc_pass}|" .env
    $SED_INPLACE "s|YOUTUBE_CLIENT_ID=your-client-id.apps.googleusercontent.com|YOUTUBE_CLIENT_ID=${yt_client_id}|" .env
    $SED_INPLACE "s|YOUTUBE_CLIENT_SECRET=your-client-secret|YOUTUBE_CLIENT_SECRET=${yt_client_secret}|" .env
    $SED_INPLACE "s|YOUTUBE_REFRESH_TOKEN=your-refresh-token|YOUTUBE_REFRESH_TOKEN=${yt_refresh_token}|" .env

    if [ -n "$discord_webhook" ]; then
        $SED_INPLACE "s|NOTIFICATION_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...|NOTIFICATION_DISCORD_WEBHOOK_URL=${discord_webhook}|" .env
    fi

    info ".env configured successfully."
fi

# ---------------------------------------------------------------------------
# Build and start
# ---------------------------------------------------------------------------
echo ""
info "Building Docker image..."
$COMPOSE build

info "Starting containers..."
$COMPOSE up -d

echo ""
echo "========================================"
echo -e "  ${GREEN}fade-out is running!${NC}"
echo ""

# Detect IP for access URL
if command -v hostname &>/dev/null; then
    HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
else
    HOST_IP="localhost"
fi

echo "  Dashboard: http://${HOST_IP}:8500"
echo "  Logs:      $COMPOSE logs -f fade-out"
echo "========================================"
