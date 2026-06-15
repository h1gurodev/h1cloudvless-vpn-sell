"""Async client for the H1Cloud VLESS node API (see API Docs).

All endpoints are under the /api prefix and return JSON with an `ok` field.
Auth is a Bearer token. The node may use a self-signed cert, so SSL
verification is configurable.
"""
from __future__ import annotations

import logging
import re
import ssl
from urllib.parse import quote

import aiohttp

log = logging.getLogger("vpnsell.h1cloud")


def sanitize_name(value: str, fallback: str = "client") -> str:
    """The node only accepts [A-Za-z0-9._-] names (see 400 bad_name)."""
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    text = re.sub(r"_+", "_", text).strip("._-")
    return (text or fallback)[:64]


class H1CloudError(Exception):
    """Raised when the node returns ok:false or is unreachable."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class H1CloudClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify_ssl: bool = False,
        timeout: int = 20,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._ssl: ssl.SSLContext | bool = True if verify_ssl else False
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._token)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._token}",
                }
            )
        return self._session

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if not self.configured:
            raise H1CloudError("H1Cloud API is not configured", code="not_configured")
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with session.request(
                method, url, ssl=self._ssl, timeout=timeout, **kwargs
            ) as resp:
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            log.warning("H1Cloud %s %s network error: %s", method, url, exc)
            raise H1CloudError(f"Узел недоступен: {exc}", code="unreachable") from exc
        except Exception as exc:  # noqa: BLE001 - bad JSON, etc.
            log.warning("H1Cloud %s %s error: %s", method, url, exc)
            raise H1CloudError("Некорректный ответ узла", code="bad_response") from exc

        if not isinstance(data, dict):
            raise H1CloudError("Некорректный ответ узла", code="bad_response")
        if not data.get("ok", False):
            code = str(data.get("error") or "unknown")
            log.info("H1Cloud %s %s returned ok:false code=%s", method, url, code)
            raise H1CloudError(f"Узел вернул ошибку: {code}", code=code)
        return data

    # --- methods ---

    async def health(self) -> dict:
        return await self._request("GET", "/health")

    async def get_client(self, name: str) -> dict | None:
        """Return the client dict, or None if it doesn't exist."""
        try:
            data = await self._request(
                "GET", f"/clients/{quote(sanitize_name(name), safe='')}"
            )
        except H1CloudError as exc:
            if exc.code == "user_not_found":
                return None
            raise
        client = data.get("client")
        return client if isinstance(client, dict) else data

    async def create_client(
        self,
        name: str,
        days: int,
        *,
        traffic_limit_gb: int = 0,
        device_limit: int = 0,
    ) -> dict:
        payload: dict[str, object] = {"name": sanitize_name(name), "days": max(1, days)}
        if traffic_limit_gb > 0:
            payload["traffic_limit_gb"] = traffic_limit_gb
        if device_limit > 0:
            payload["device_limit"] = device_limit
        return await self._request("POST", "/create", json=payload)

    async def renew_client(self, name: str, add_days: int) -> dict:
        """Extend a client; `days` is added to the current expiry by the node."""
        payload = {"name": sanitize_name(name), "days": max(1, add_days)}
        return await self._request("PATCH", "/edit", json=payload)

    async def set_limits(
        self, name: str, *, traffic_limit_gb: int | None = None, device_limit: int | None = None
    ) -> dict:
        payload: dict[str, object] = {"name": sanitize_name(name)}
        if traffic_limit_gb is not None:
            payload["traffic_limit_gb"] = traffic_limit_gb
        if device_limit is not None:
            payload["device_limit"] = device_limit
        return await self._request("PATCH", "/edit", json=payload)

    async def delete_client(self, name: str) -> bool:
        try:
            await self._request(
                "DELETE", f"/clients/{quote(sanitize_name(name), safe='')}"
            )
            return True
        except H1CloudError as exc:
            if exc.code == "user_not_found":
                return True
            raise

    async def ban_client(self, name: str, reason: str = "") -> dict:
        return await self._request(
            "PATCH",
            f"/clients/{quote(sanitize_name(name), safe='')}/ban",
            json={"reason": reason},
        )

    async def unban_client(self, name: str) -> dict:
        return await self._request(
            "PATCH", f"/clients/{quote(sanitize_name(name), safe='')}/unban"
        )

    async def get_links(self, name: str) -> list[str]:
        client = await self.get_client(name)
        if not client:
            return []
        result: list[str] = []
        links = client.get("links")
        if isinstance(links, dict):
            for value in links.values():
                if isinstance(value, str) and value.startswith("vless://"):
                    result.append(value)
        elif isinstance(links, list):
            result.extend(v for v in links if isinstance(v, str) and v.startswith("vless://"))
        link = client.get("link")
        if isinstance(link, str) and link.startswith("vless://") and link not in result:
            result.append(link)
        return result

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
