"""Async client for the WAVIoT LK HTTP API (lk.waviot.ru)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class WaviotApiError(Exception):
    """Generic API error."""


class WaviotAuthError(WaviotApiError):
    """Invalid API key / no access to modem."""


class WaviotApiClient:
    """Minimal wrapper around https://lk.waviot.ru/api.TYPE/METHOD/ endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        base_url: str = "https://lk.waviot.ru",
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def _request(self, api_type: str, method: str, **params: Any) -> dict:
        url = f"{self._base_url}/api.{api_type}/{method}/"
        params = {k: v for k, v in params.items() if v is not None}
        params["key"] = self._api_key
        try:
            async with asyncio.timeout(30):
                async with self._session.get(url, params=params) as resp:
                    if resp.status in (401, 403):
                        raise WaviotAuthError(f"HTTP {resp.status} for {url}")
                    resp.raise_for_status()
                    try:
                        data = await resp.json(content_type=None)
                    except Exception as err:  # noqa: BLE001 - HTML error pages etc.
                        text = (await resp.text())[:200]
                        raise WaviotApiError(
                            f"Non-JSON response from {method}: {text!r}"
                        ) from err
        except WaviotApiError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise WaviotApiError(f"Request to {url} failed: {err}") from err

        if not isinstance(data, dict):
            raise WaviotApiError(f"Unexpected response from {url}: {data!r}")
        if data.get("status") == "error":
            message = data.get("message") or data.get("error") or "unknown error"
            if "key" in str(message).lower() or "auth" in str(message).lower():
                raise WaviotAuthError(message)
            raise WaviotApiError(f"API error from {method}: {message}")
        return data

    async def modem_info(self, modem_id: str) -> dict:
        data = await self._request("modem", "info", id=modem_id)
        modem = data.get("modem")
        if not modem:
            raise WaviotApiError(f"No modem info returned for {modem_id}")
        return modem

    async def last_message(self, modem_id: str) -> dict | None:
        try:
            data = await self._request("modem", "get_last_message", id=modem_id)
        except WaviotApiError as err:
            _LOGGER.debug("get_last_message failed: %s", err)
            return None
        return data.get("packet")

    async def get_values(
        self, modem_id: str, ts_from: int | None = None, ts_to: int | None = None
    ) -> dict:
        """Return raw get_values payload (all registrators of the modem)."""
        return await self._request(
            "data", "get_values", modem_id=modem_id, **{"from": ts_from, "to": ts_to}
        )

    async def channel_values(
        self,
        modem_id: str,
        channel: str,
        ts_from: int | None = None,
        ts_to: int | None = None,
    ) -> dict[int, float]:
        """Return {unix_ts: cumulative_value} for one channel, sorted by ts."""
        data = await self._request(
            "data",
            "get_modem_channel_values",
            modem_id=modem_id,
            channel=channel,
            **{"from": ts_from, "to": ts_to},
        )
        raw = data.get("values") or {}
        out: dict[int, float] = {}
        if isinstance(raw, dict):
            items = raw.items()
        elif isinstance(raw, list):
            # Some deployments return a list of {timestamp, value} objects.
            items = (
                (item.get("timestamp"), item.get("value"))
                for item in raw
                if isinstance(item, dict)
            )
        else:
            return out
        for ts, val in items:
            try:
                ts_i = int(ts)
                if ts_i > 10**12:  # milliseconds
                    ts_i //= 1000
                out[ts_i] = float(val)
            except (TypeError, ValueError):
                continue
        return dict(sorted(out.items()))
