#!/usr/bin/env python3
"""
X GraphQL Scraper — Auto-monitor de progreso
Ejecuta el scraper, verifica avance por COUNT de tweets, no líneas.
"""
import sys
import time
import subprocess
import json
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).parent.parent.absolute()
DATA_DIR = BASE / "data" / "tweets" / "graphql"
STATE_FILE = BASE / "data" / "scraper_state.json"
LOG_FILE = BASE / "data" / "logs" / "supervisor.log"
PYTHON = BASE / "venv" / "bin" / "python"
SCRIPT = BASE / "cron_entry.py"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

TARGETS = ["financialjuice", "Deltaone"]


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def count_tweets(target: str) -> int:
    """Cuenta tweets reales en el JSON array."""
    path = DATA_DIR / f"{target}_all.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return len(data)
    except Exception:
        pass
    return 0


def run_scraper() -> tuple[int, str]:
    """Ejecuta el scraper. Retorna (exit_code, output_summary)."""
    log("[▶] Running scraper...")
    try:
        result = subprocess.run(
            [str(PYTHON), str(SCRIPT)],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            timeout=600,  # 10 min — el cron no tiene timeout
        )
        
        # Extract last lines of output
        stdout_lines = result.stdout.strip().split("\n")[-15:] if result.stdout else []
        for line in stdout_lines:
            log(f"  [→] {line.strip()[:200]}")
        
        log(f"[✓] Exit code: {result.returncode}")
        return result.returncode
    except subprocess.TimeoutExpired:
        log("[!] Scraper timed out after 10 minutes")
        return 1
    except Exception as e:
        log(f"[!] Exception: {e}")
        return 1


def main():
    state = get_state()
    before = {t: count_tweets(t) for t in TARGETS}
    log("=" * 50)
    log(f"Supervisor started")
    log(f"Tweet counts before: {before}")
    
    # Run scraper
    exit_code = run_scraper()
    
    # Check progress
    after = {t: count_tweets(t) for t in TARGETS}
    log(f"Tweet counts after:  {after}")
    
    progress = {}
    for target in TARGETS:
        gained = after.get(target, 0) - before.get(target, 0)
        progress[target] = gained
        if gained > 0:
            log(f"  [↑] @{target}: +{gained} tweets")
        else:
            log(f"  [→] @{target}: no change ({after.get(target, 0)} total)")
    
    # Update state
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["progress"] = progress
    state["exit_code"] = exit_code
    state["counts"] = after
    
    if exit_code == 0:
        state["consecutive_failures"] = 0
        log("[✓] Scraper completed successfully")
    else:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        log(f"[!] Scraper failed. Consecutive failures: {state['consecutive_failures']}")
    
    save_state(state)
    log("=" * 50)


if __name__ == "__main__":
    main()
