"""Client for the 2328.io cryptocurrency payment API.

Requests are signed with HMAC-SHA256 over the Base64 of the compact JSON body
(empty string for bodyless GETs), using the project API key. Webhook payloads
are verified with the same algorithm. See https://doc.2328.io.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging

import aiohttp

log = logging.getLogger("vpnsell.payments")


def _canonical_json(data: dict) -> str:
    # Compact (no whitespace), non-ASCII preserved — must match what the
    # server signs, or the signature won't verify.
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def sign_body(data: dict | None, api_key: str) -> str:
    """HMAC-SHA256 of base64(compact-json), lowercase hex. Empty body -> sign ''."""
    payload = _canonical_json(data) if data else ""
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii") if payload else ""
    return hmac.new(api_key.encode("utf-8"), b64.encode("ascii"), hashlib.sha256).hexdigest()


def verify_webhook(payload: dict, api_key: str) -> bool:
    """Verify a webhook: strip `sign`, recompute, constant-time compare."""
    received = str(payload.get("sign") or "")
    if not received:
        return False
    body = {k: v for k, v in payload.items() if k != "sign"}
    expected = sign_body(body, api_key)
    return hmac.compare_digest(expected, received)


class PaymentError(Exception):
    pass


class PaymentClient:
    def __init__(
        self,
        base_url: str,
        project_uuid: str,
        api_key: str,
        *,
        user_agent: str,
        timeout: int = 20,
    ):
        self._base_url = base_url.rstrip("/")
        self._project = project_uuid
        self._api_key = api_key
        self._user_agent = user_agent
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._project and self._api_key)

    @property
    def api_key(self) -> str:
        return self._api_key

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _post(self, path: str, body: dict) -> dict:
        if not self.configured:
            raise PaymentError("Payment API is not configured")
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
            "project": self._project,
            "sign": sign_body(body, self._api_key),
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        # Send the exact bytes we signed; don't let aiohttp re-serialize.
        raw = _canonical_json(body).encode("utf-8")
        try:
            async with session.post(url, data=raw, headers=headers, timeout=timeout) as resp:
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            log.warning("2328 %s network error: %s", url, exc)
            raise PaymentError(f"Платёжная система недоступна: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            log.warning("2328 %s bad response: %s", url, exc)
            raise PaymentError("Некорректный ответ платёжной системы") from exc

        if not isinstance(data, dict) or data.get("state") != 0:
            log.info("2328 %s returned %s", url, data)
            raise PaymentError("Платёжная система отклонила запрос")
        return data.get("result") or {}

    async def create_payment(
        self,
        *,
        amount: int,
        currency: str,
        order_id: str,
        callback_url: str = "",
        description: str = "",
        ttl_seconds: int = 3600,
    ) -> dict:
        body: dict[str, object] = {
            "amount": f"{amount}.00",
            "currency": currency,
            "order_id": order_id,
            "ttl_seconds": ttl_seconds,
            "url_callback": 'https://h1cloud.su',
        }
        if callback_url:
            body["url_callback"] = callback_url
        if description:
            body["description"] = description[:200]
        return await self._post("/v1/payment", body)

    async def payment_info(self, *, uuid: str = "", order_id: str = "") -> dict:
        body: dict[str, object] = {}
        if uuid:
            body["uuid"] = uuid
        if order_id:
            body["order_id"] = order_id
        return await self._post("/v1/payment/info", body)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
