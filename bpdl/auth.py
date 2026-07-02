from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

CLIENT_ID = "ryZ8LuyQVPqbK2mBX2Hwt4qSMtnWuTYSqBPO92yQ"
TOKEN_ENDPOINT = "/auth/o/token/"
AUTH_ENDPOINT = f"/auth/o/authorize/?client_id={CLIENT_ID}&response_type=code"
LOGIN_ENDPOINT = "/auth/login/"


class AuthError(RuntimeError):
    pass


@dataclass
class TokenPair:
    access_token: str = ""
    refresh_token: str = ""
    expires_in: int = 0
    token_type: str = ""
    scope: str = ""
    login_id: str = ""
    issued_at: int = 0

    @classmethod
    def from_json(cls, data: dict) -> "TokenPair":
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_in=data.get("expires_in", 0),
            token_type=data.get("token_type", ""),
            scope=data.get("scope", ""),
            login_id=data.get("login_id", ""),
            issued_at=data.get("issued_at", 0),
        )


class Auth:
    def __init__(self, username: str, password: str, cache_file: Path):
        self.username = username
        self.password = password
        self.cache_file = cache_file
        self.token: TokenPair | None = None
        self._lock = threading.RLock()

    def login_id(self) -> str:
        data = f"{self.username}:{self.password}".encode()
        # FNV-1a 64-bit, matching the Go implementation's cache-invalidation hash
        h = 0xCBF29CE484222325
        for b in data:
            h ^= b
            h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        return h.to_bytes(8, "big").hex()

    def load_cache(self) -> bool:
        try:
            data = json.loads(self.cache_file.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        token = TokenPair.from_json(data)
        if token.login_id != self.login_id():
            return False
        self.token = token
        return True

    def write_cache(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(asdict(self.token), indent=1))
        self.cache_file.chmod(0o600)

    def invalidate(self) -> None:
        with self._lock:
            if self.token:
                self.token.issued_at = 0

    def check(self, client) -> None:
        with self._lock:
            expires_at = (self.token.issued_at + self.token.expires_in) if self.token else 0
            if time.time() + 300 >= expires_at:
                try:
                    self._refresh(client)
                except Exception:
                    self._init(client)

    def _init(self, client) -> None:
        session_id = self._login(client)
        code = self._authorize(client, session_id)
        self._issue(client, code)

    def init(self, client) -> None:
        with self._lock:
            self._init(client)

    def _login(self, client) -> str:
        payload = {"username": self.username, "password": self.password}
        resp = client.raw_fetch("POST", LOGIN_ENDPOINT, payload, "application/json")
        session_id = resp.cookies.get("sessionid")
        if not session_id:
            raise AuthError("invalid session cookie")
        return session_id

    def _authorize(self, client, session_id: str) -> str:
        client.headers["cookie"] = f"sessionid={session_id}"
        try:
            resp = client.raw_fetch("GET", AUTH_ENDPOINT, None, "", allow_redirects=False)
        finally:
            client.headers.pop("cookie", None)
        location = resp.headers.get("Location", "")
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(location).query)
        code = query.get("code", [""])[0]
        if not code:
            raise AuthError("invalid authorization code")
        return code

    def _issue(self, client, code: str) -> None:
        payload = {"client_id": CLIENT_ID, "grant_type": "authorization_code", "code": code}
        resp = client.raw_fetch("POST", TOKEN_ENDPOINT, payload, "application/x-www-form-urlencoded")
        self.token = TokenPair.from_json(resp.json())
        self.token.issued_at = int(time.time())
        self.token.login_id = self.login_id()
        self.write_cache()

    def _refresh(self, client) -> None:
        if not self.token or not self.token.refresh_token:
            raise AuthError("no refresh token")
        payload = {
            "client_id": CLIENT_ID,
            "refresh_token": self.token.refresh_token,
            "grant_type": "refresh_token",
        }
        resp = client.raw_fetch("POST", TOKEN_ENDPOINT, payload, "application/x-www-form-urlencoded")
        login_id = self.token.login_id
        self.token = TokenPair.from_json(resp.json())
        self.token.issued_at = int(time.time())
        self.token.login_id = login_id
        self.write_cache()
