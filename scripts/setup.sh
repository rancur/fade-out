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
# NAS directory creation (if paths exist)
# ---------------------------------------------------------------------------
NAS_DIRS=(
    "/volume1/will-see/YouTube Thumbnails"
    "/volume1/will-see/SoundCloud Cover Art"
)

for dir in "${NAS_DIRS[@]}"; do
    parent=$(dirname "$dir")
    if [ -d "$parent" ]; then
        if [ ! -d "$dir" ]; then
            info "Creating NAS directory: $dir"
            mkdir -p "$dir"
        else
            info "NAS directory exists: $dir"
        fi
    fi
done

# ---------------------------------------------------------------------------
# Local data directory
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

info "Creating local data directory..."
mkdir -p data

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
    $SED_INPLACE "s|SOUNDCLOUD_EMAIL=your@email.com|SOUNDCLOUD_EMAIL=${sc_email}|" .env
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
