# src/protocols/token_store.py
"""
Persistent store for the MCP server's OAuth 2.1 authorization data.

Single-user scale: clients, authorization codes, access tokens, and refresh
tokens live in one JSON file under DATA_DIR/oauth/. The file is gitignored
(data/*) and survives process restarts; a docker volume keeps it across
container recreations. All writes are atomic (tmp file + rename) and guarded
by a lock; expired entries are pruned opportunistically on write.
"""
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("de-job-intelligence.oauth.store")

# Lifetimes (seconds)
AUTH_CODE_TTL = 600          # 10 minutes, single use
ACCESS_TOKEN_TTL = 3600      # 1 hour
REFRESH_TOKEN_TTL = 30 * 86400  # 30 days


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {
            "clients": {},
            "codes": {},
            "access": {},
            "refresh": {},
        }
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not load OAuth store %s: %s", self.path, exc)

    def _save(self) -> None:
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tokens-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _prune(self) -> None:
        now = time.time()
        for bucket in ("codes", "access", "refresh"):
            expired = [k for k, v in self._data[bucket].items() if v.get("expires_at", 0) <= now]
            for k in expired:
                del self._data[bucket][k]

    # -- OAuth clients (RFC 7591) ------------------------------------------

    def register_client(
        self, redirect_uris: list, client_name: str = "", scope: str = ""
    ) -> Dict[str, str]:
        client_id = f"mcp-{secrets.token_hex(8)}"
        client_secret = secrets.token_urlsafe(32)
        with self._lock:
            self._data["clients"][client_id] = {
                "client_secret": client_secret,
                "redirect_uris": redirect_uris,
                "client_name": client_name,
                "scope": scope,
                "created_at": time.time(),
            }
            self._save()
        return {"client_id": client_id, "client_secret": client_secret}

    def get_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        return self._data["clients"].get(client_id)

    # -- authorization codes ------------------------------------------------

    def create_code(
        self,
        client_id: str,
        redirect_uri: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._data["codes"][code] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": scope,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "expires_at": time.time() + AUTH_CODE_TTL,
            }
            self._save()
        return code

    def consume_code(self, code: str) -> Optional[Dict[str, Any]]:
        """Single-use: returns the code record and deletes it."""
        with self._lock:
            record = self._data["codes"].pop(code, None)
            if record is None:
                return None
            if record.get("expires_at", 0) <= time.time():
                self._save()
                return None
            self._save()
            return record

    # -- tokens --------------------------------------------------------------

    def create_token_pair(self, client_id: str, scope: str) -> Dict[str, Any]:
        now = time.time()
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        with self._lock:
            self._data["access"][access_token] = {
                "client_id": client_id,
                "scope": scope,
                "expires_at": now + ACCESS_TOKEN_TTL,
            }
            self._data["refresh"][refresh_token] = {
                "client_id": client_id,
                "scope": scope,
                "expires_at": now + REFRESH_TOKEN_TTL,
            }
            self._save()
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": ACCESS_TOKEN_TTL,
        }

    def get_access_token(self, token: str) -> Optional[Dict[str, Any]]:
        record = self._data["access"].get(token)
        if record is None or record.get("expires_at", 0) <= time.time():
            return None
        return record

    def rotate_refresh_token(
        self, refresh_token: str, client_id: str
    ) -> Optional[Dict[str, Any]]:
        """Refresh-token rotation: single-use old token, fresh pair issued."""
        with self._lock:
            record = self._data["refresh"].pop(refresh_token, None)
            if record is None or record.get("expires_at", 0) <= time.time():
                self._save()
                return None
            if record.get("client_id") != client_id:
                self._save()
                return None
            self._save()
        return self.create_token_pair(client_id, record.get("scope", ""))


_store: Optional[TokenStore] = None


def get_store() -> TokenStore:
    """Process-wide singleton, rooted at DATA_DIR/oauth/tokens.json."""
    global _store
    if _store is None:
        from src.core.config import settings

        _store = TokenStore(settings.DATA_DIR / "oauth" / "tokens.json")
    return _store
