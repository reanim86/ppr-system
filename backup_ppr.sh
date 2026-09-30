#!/usr/bin/env bash
# Резервное копирование базы данных ППР. Запускается по cron (см. deploy.sh).
# Копирование делается через SQLite API, чтобы файл не повредился при активной записи.

set -euo pipefail

APP_DIR="/opt/ppr-system"
BACKUP_DIR="/var/backups/ppr"
KEEP=30
SRC="$APP_DIR/ppr.db"

if [ ! -f "$SRC" ]; then
  exit 0
fi

DST="$BACKUP_DIR/ppr_$(date +%F).db"
"$APP_DIR/venv/bin/python" - "$SRC" "$DST" <<'PY'
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
try:
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
except sqlite3.OperationalError:
    pass
PY

# Оставляем только последние KEEP копий
ls -1t "$BACKUP_DIR"/ppr_*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f