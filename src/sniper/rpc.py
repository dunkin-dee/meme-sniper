"""Minimal async Solana JSON-RPC client.

Scope is deliberately small - ``getAccountInfo`` and ``getMultipleAccounts`` are
all Phase 1 needs - but the rate limiting and batching live here rather than at
the call site because #6's curve tracker will run the same two methods against a
hard credit budget.

Two constraints shape the design:

* **Free-tier RPS is the binding limit, not bandwidth.** Helius free tier allows
  10 RPS; ``rpc.max_rps`` sits under that deliberately. The limiter is a simple
  monotonic-clock spacer rather than a token bucket - bursting is exactly what
  gets an API key throttled.
* **``getMultipleAccounts`` caps at 100 accounts per call** and returns results
  positionally, including nulls for accounts that do not exist. Callers depend
  on that alignment, so the chunking must never reorder or drop entries.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config

log = logging.getLogger("sniper.rpc")


class RpcError(RuntimeError):
    """Raised when the RPC endpoint returns an error or malformed response."""


@dataclass(frozen=True)
class AccountInfo:
    """A decoded account, or the fact that it does not exist."""

    pubkey: str
    owner: str
    lamports: int
    data: bytes
    executable: bool

    @property
    def size(self) -> int:
        return len(self.data)


class SolanaRpc:
    """Rate-limited JSON-RPC client with a fallback endpoint.

    The fallback matters because ``rpc.url`` interpolates ${HELIUS_API_KEY};
    with no key set it expands to a URL with an empty api-key that fails at
    request time rather than at config load. Falling back to the public endpoint
    keeps the diagnostic runnable on a fresh checkout.
    """

    def __init__(self, cfg: Config) -> None:
        self.url: str = cfg.get("rpc.url")
        self.fallback_url: str = cfg.get("rpc.fallback_url")
        self.timeout: float = float(cfg.get("rpc.timeout_seconds"))
        self.max_rps: float = float(cfg.get("rpc.max_rps"))
        self.max_accounts_per_call: int = int(cfg.get("rpc.max_accounts_per_call"))

        # An unexpanded ${...} or an empty api-key means no key was provided.
        if "api-key=" in self.url and self.url.rstrip().endswith("api-key="):
            log.info("no HELIUS_API_KEY set; using public RPC endpoint")
            self.url = self.fallback_url

        self._min_interval = 1.0 / self.max_rps if self.max_rps > 0 else 0.0
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0
        self.calls = 0

    async def __aenter__(self) -> SolanaRpc:
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _throttle(self) -> None:
        """Space requests to stay under max_rps. Held across the await on purpose."""
        async with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()

    async def _call(self, method: str, params: list[Any]) -> Any:
        if self._client is None:
            raise RpcError("SolanaRpc must be used as an async context manager")

        await self._throttle()
        self._request_id += 1
        self.calls += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }

        last_exc: Exception | None = None
        for url in (self.url, self.fallback_url):
            try:
                resp = await self._client.post(url, json=body)
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                log.debug("rpc %s failed against %s: %s", method, url, exc)
                continue

            if "error" in payload:
                # A JSON-RPC error is a real answer, not a transport fault -
                # retrying it against the fallback would just repeat it.
                raise RpcError(f"{method}: {payload['error']}")
            return payload.get("result")

        raise RpcError(f"{method}: all endpoints failed ({last_exc})")

    async def get_account_info(self, pubkey: str) -> AccountInfo | None:
        """Fetch one account. Returns None if it does not exist on chain."""
        result = await self._call(
            "getAccountInfo", [pubkey, {"encoding": "base64", "commitment": "confirmed"}]
        )
        value = (result or {}).get("value")
        return _parse_account(pubkey, value)

    async def get_multiple_accounts(self, pubkeys: list[str]) -> list[AccountInfo | None]:
        """Fetch many accounts, preserving input order (None where absent).

        Chunked at rpc.max_accounts_per_call - the RPC rejects larger batches
        outright rather than truncating them.
        """
        out: list[AccountInfo | None] = []
        for i in range(0, len(pubkeys), self.max_accounts_per_call):
            chunk = pubkeys[i : i + self.max_accounts_per_call]
            result = await self._call(
                "getMultipleAccounts",
                [chunk, {"encoding": "base64", "commitment": "confirmed"}],
            )
            values = (result or {}).get("value") or []
            # Positional alignment is load-bearing; pad rather than zip short.
            for key, value in zip(chunk, list(values) + [None] * (len(chunk) - len(values))):
                out.append(_parse_account(key, value))
        return out


def _parse_account(pubkey: str, value: dict[str, Any] | None) -> AccountInfo | None:
    if not value:
        return None
    raw = value.get("data")
    if isinstance(raw, list) and raw:
        data = base64.b64decode(raw[0])
    elif isinstance(raw, str):
        data = base64.b64decode(raw)
    else:
        data = b""
    return AccountInfo(
        pubkey=pubkey,
        owner=value.get("owner", ""),
        lamports=int(value.get("lamports", 0)),
        data=data,
        executable=bool(value.get("executable", False)),
    )
