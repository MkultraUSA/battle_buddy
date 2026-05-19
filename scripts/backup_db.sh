#!/bin/bash
# Backup Battle Buddy SQLite DB daily
set -euo pipefail
SOURCE_DB="${BATTLE_BUDDY_DB:-/opt/battlebuddy/calls.db}"
BACKUP_DIR="/opt/data/battle_buddy/backups"
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
BACKUP_FILE="$BACKUP_DIR/calls_backup_${TIMESTAMP}.db"
# Use SQLite backup API to ensure consistency
sqlite3 "$SOURCE_DB" ".backup '$BACKUP_FILE'"
# Keep last 7 backups
ls -1t "$BACKUP_DIR"/calls_backup_*.db | tail -n +8 | xargs -r rm --
echo "Backup created at $BACKUP_FILE"
