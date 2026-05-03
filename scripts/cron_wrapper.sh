#!/bin/bash
# X GraphQL Scraper Cron Wrapper — Nunca se rinde
# Ejecuta el scraper y si falla, reintenta hasta 3 veces

BASE="/home/cristian/projects/x-v2-collector"
VENV="$BASE/venv/bin/python"
SCRIPT="$BASE/cron_entry.py"
LOG="$BASE/data/logs/cron_wrapper.log"

mkdir -p "$BASE/data/logs"

cd "$BASE" || exit 1

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] Starting cron run" >> "$LOG"

for i in 1 2 3; do
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] Attempt $i..." >> "$LOG"
    
    if $VENV "$SCRIPT" >> "$LOG" 2>&1; then
        echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] ✓ Success on attempt $i" >> "$LOG"
        exit 0
    fi
    
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] ✗ Failed attempt $i, retrying..." >> "$LOG"
    sleep 30
done

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] ✗ All attempts failed" >> "$LOG"
exit 1
