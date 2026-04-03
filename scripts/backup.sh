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
DB_FILE="data/fade-out.db"
if [ -f "$DB_FILE" ]; then
    info "Backing up database..."
    # Use sqlite3 .backup for a safe copy (handles WAL mode correctly)
    if command -v sqlite3 &>/dev/null; then
        sqlite3 "$DB_FILE" ".backup '${STAGING_DIR}/fade-out.db'"
    else
        # Fallback: try docker exec if sqlite3 not available locally
        if docker exec fade-out sqlite3 /data/fade-out.db ".backup '/tmp/fade-out-backup.db'" 2>/dev/null; then
            docker cp fade-out:/tmp/fade-out-backup.db "${STAGING_DIR}/fade-out.db"
            docker exec fade-out rm /tmp/fade-out-backup.db
        else
            warn "sqlite3 not available. Copying database file directly (may be inconsistent if writes are in progress)."
            cp "$DB_FILE" "${STAGING_DIR}/fade-out.db"
        fi
    fi
    info "Database backed up."
else
    warn "No database found at ${DB_FILE}. Skipping."
fi

# Backup WAL and SHM files if they exist
for ext in db-wal db-shm; do
    if [ -f "data/fade-out.${ext}" ]; then
        cp "data/fade-out.${ext}" "${STAGING_DIR}/"
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
echo "    cp ${BACKUP_NAME}/fade-out.db data/"
echo "    cp ${BACKUP_NAME}/.env ."
echo "========================================"
