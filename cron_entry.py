#!/usr/bin/env python3
"""Entry point para cron — ejecuta el scraper GraphQL en un solo run."""
import sys
from pathlib import Path

BASE = Path(__file__).parent.absolute()
ENGINE_DIR = BASE / "src" / "engine"
sys.path.insert(0, str(ENGINE_DIR))
sys.path.insert(0, str(BASE / "src"))

from x_graphql_scraper import run_all_targets

if __name__ == "__main__":
    results = run_all_targets()
    any_success = any(r.get("success", False) for r in results.values())
    sys.exit(0 if any_success else 1)
