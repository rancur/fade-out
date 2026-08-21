#!/usr/bin/env bash
# ==============================================================================
# fade-out — Backup script
# Backs up SQLite database, .env, and brand settings to a timestamped archive
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
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="${PROJECT_DIR}/backups"
BACKUP_NAME="fade-out-backup-${TIMESTAMP}"
STAGING_DIR="${BACKUP_DIR}/${BACKUP_NAME}"

cd "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# Create backup staging directory
# ---------------------------------------------------------------------------
mkdir -p "$STAGING_DIR"

info "Starting backup: ${BACKUP_NAME}"

# ---------------------------------------------------------------------------
# Backup SQLite database
# ---------------------------------------------------------------------------
# The application writes /data/fadeout.db (see DATABASE_URL in
# backend/app/config.py), which is bind-mounted to ./data on the host.
DB_NAME="${DB_NAME:-fadeout.db}"
DB_FILE="data/${DB_NAME}"

if [ -f "$DB_FILE" ]; then
    info "Backing up database..."
    # Use sqlite3 .backup for a safe copy (handles WAL mode correctly)
    if command -v sqlite3 &>/dev/null; then
        sqlite3 "$DB_FILE" ".backup '${STAGING_DIR}/${DB_NAME}'"
    elif docker exec fade-out sqlite3 "/data/${DB_NAME}" ".backup '/tmp/${DB_NAME}.backup'" 2>/dev/null; then
        docker cp "fade-out:/tmp/${DB_NAME}.backup" "${STAGING_DIR}/${DB_NAME}"
        docker exec fade-out rm "/tmp/${DB_NAME}.backup"
    else
        warn "sqlite3 not available. Copying database file directly (may be inconsistent if writes are in progress)."
        cp "$DB_FILE" "${STAGING_DIR}/${DB_NAME}"
    fi
    info "Database backed up."
else
    # A "backup" with no database in it is not a backup. Fail loudly rather
    # than printing "Backup complete!" over an archive that cannot restore.
    error "No database found at ${DB_FILE}.
       Run this from the project root, after the app has created its database.
       If your database lives elsewhere, set DB_NAME (file name under ./data)."
fi

# Backup WAL and SHM files if they exist
DB_STEM="${DB_NAME%.db}"
for ext in db-wal db-shm; do
    if [ -f "data/${DB_STEM}.${ext}" ]; then
        cp "data/${DB_STEM}.${ext}" "${STAGING_DIR}/"
    fi
done

# ---------------------------------------------------------------------------
# Backup environment file
# ---------------------------------------------------------------------------
if [ -f .env ]; then
    info "Backing up .env..."
    cp .env "${STAGING_DIR}/.env"
else
    warn "No .env file found. Skipping."
fi

# ---------------------------------------------------------------------------
# Backup brand settings (if stored as separate files)
# ---------------------------------------------------------------------------
BRAND_DIR="data/brand"
if [ -d "$BRAND_DIR" ]; then
    info "Backing up brand settings..."
    cp -r "$BRAND_DIR" "${STAGING_DIR}/brand"
fi

# Also grab any uploaded logos/assets
ASSETS_DIR="data/assets"
if [ -d "$ASSETS_DIR" ]; then
    info "Backing up brand assets..."
    cp -r "$ASSETS_DIR" "${STAGING_DIR}/assets"
fi

# ---------------------------------------------------------------------------
# Backup YouTube OAuth tokens (if stored on disk)
# ---------------------------------------------------------------------------
TOKENS_FILE="data/youtube-tokens.json"
if [ -f "$TOKENS_FILE" ]; then
    info "Backing up YouTube tokens..."
    cp "$TOKENS_FILE" "${STAGING_DIR}/youtube-tokens.json"
fi

# ---------------------------------------------------------------------------
# Compress
# ---------------------------------------------------------------------------
info "Compressing backup..."
cd "$BACKUP_DIR"
tar -czf "${BACKUP_NAME}.tar.gz" "$BACKUP_NAME"

# Remove staging directory
rm -rf "$STAGING_DIR"

BACKUP_FILE="${BACKUP_DIR}/${BACKUP_NAME}.tar.gz"
BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)

echo ""
echo "========================================"
echo -e "  ${GREEN}Backup complete!${NC}"
echo ""
echo "  File: ${BACKUP_FILE}"
echo "  Size: ${BACKUP_SIZE}"
echo ""
echo "  Restore with:"
echo "    tar -xzf ${BACKUP_NAME}.tar.gz"
echo "    cp ${BACKUP_NAME}/${DB_NAME} data/"
echo "    cp ${BACKUP_NAME}/.env ."
echo "========================================"
