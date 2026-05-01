"""
Session manager — persists cookies to disk for resilient re-authentication.
"""
import json
import os
from pathlib import Path
from typing import Optional, Dict, Any
import structlog

logger = structlog.get_logger("session_manager")


class SessionManager:
    """Manages cookie persistence for the twikit client.

    Cookie file is stored as JSON on disk and survives container restarts
    when mounted as a Docker volume.
    """

    def __init__(self, cookies_path: str = "/app/data/cookies.json"):
        self.cookies_path = Path(cookies_path)
        self._ensure_dir()

    def _ensure_dir(self):
        self.cookies_path.parent.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.cookies_path.exists() and self.cookies_path.stat().st_size > 0

    def load(self) -> Optional[Dict[str, Any]]:
        """Load cookies JSON from disk. Return None if missing/invalid."""
        if not self.exists():
            logger.info("no_cookies_found", path=str(self.cookies_path))
            return None
        try:
            with open(self.cookies_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info("cookies_loaded", path=str(self.cookies_path))
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("cookies_corrupted", path=str(self.cookies_path), error=str(exc))
            self._safe_remove()
            return None

    def save(self, cookies: Dict[str, Any]):
        """Save cookies JSON to disk atomically."""
        self._ensure_dir()
        tmp_path = self.cookies_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.cookies_path)
            logger.info("cookies_saved", path=str(self.cookies_path))
        except OSError as exc:
            logger.error("cookies_save_failed", path=str(self.cookies_path), error=str(exc))
        finally:
            tmp_path.unlink(missing_ok=True)

    def clear(self):
        """Delete cookies file; next run will require fresh login."""
        self._safe_remove()
        logger.info("cookies_cleared")

    def _safe_remove(self):
        try:
            self.cookies_path.unlink(missing_ok=True)
        except OSError:
            pass
