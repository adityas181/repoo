from __future__ import annotations

import asyncio
import inspect
import threading
import time
from typing import Any

from azure.identity import DefaultAzureCredential
from redis.auth.token import JWToken, SimpleToken, TokenInterface
from redis.credentials import StreamingCredentialProvider


class AzureEntraRedisCredentialProvider(StreamingCredentialProvider):
    """Streaming credential provider backed by Azure Entra tokens."""

    def __init__(
        self,
        *,
        scope: str,
        object_id: str | None = None,
        refresh_margin_seconds: int = 180,
    ) -> None:
        self._scope = scope
        self._object_id = (object_id or "").strip() or None
        self._refresh_margin_seconds = max(int(refresh_margin_seconds or 180), 30)

        self._credential = DefaultAzureCredential(
            exclude_environment_credential=True,
            exclude_interactive_browser_credential=True,
        )
        self._token_lock = threading.Lock()
        self._current_token: TokenInterface | None = None

        self._on_next_callback = None
        self._on_error_callback = None
        self._callback_loop: asyncio.AbstractEventLoop | None = None
        self._refresh_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def get_credentials(self):
        token = self._get_or_refresh_token()
        oid = token.try_get("oid")
        if not oid:
            msg = "Unable to determine Redis Entra object ID. Set REDIS_ENTRA_OBJECT_ID."
            raise RuntimeError(msg)

        self._start_refresh_thread_if_ready()
        return oid, token.get_value()

    async def get_credentials_async(self):
        self._capture_current_event_loop()
        return self.get_credentials()

    def on_next(self, callback):
        self._on_next_callback = callback
        self._capture_current_event_loop()
        self._start_refresh_thread_if_ready()

    def on_error(self, callback):
        self._on_error_callback = callback
        self._capture_current_event_loop()
        self._start_refresh_thread_if_ready()

    def is_streaming(self) -> bool:
        return self._refresh_thread is not None and self._refresh_thread.is_alive()

    def close(self) -> None:
        self._stop_event.set()
        if self._refresh_thread and self._refresh_thread.is_alive():
            self._refresh_thread.join(timeout=2)
        close_fn = getattr(self._credential, "close", None)
        if callable(close_fn):
            close_fn()

    def _capture_current_event_loop(self) -> None:
        try:
            self._callback_loop = asyncio.get_running_loop()
        except RuntimeError:
            return

    def _start_refresh_thread_if_ready(self) -> None:
        if self._refresh_thread is not None:
            return
        if self._on_next_callback is None or self._on_error_callback is None:
            return

        self._stop_event.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name="redis-entra-token-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    def _refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                token = self._get_or_refresh_token()
                sleep_seconds = max(
                    ((token.get_expires_at_ms() - (time.time() * 1000)) / 1000)
                    - self._refresh_margin_seconds,
                    30,
                )
                if self._stop_event.wait(sleep_seconds):
                    break

                refreshed_token = self._get_or_refresh_token(force_refresh=True)
                self._dispatch_callback(self._on_next_callback, refreshed_token)
            except Exception as exc:  # noqa: BLE001
                self._dispatch_callback(self._on_error_callback, exc)
                if self._stop_event.wait(30):
                    break

    def _get_or_refresh_token(self, *, force_refresh: bool = False) -> TokenInterface:
        with self._token_lock:
            if (
                force_refresh
                or self._current_token is None
                or self._current_token.is_expired()
            ):
                access_token = self._credential.get_token(self._scope)
                self._current_token = self._to_redis_token(
                    token_value=access_token.token,
                    expires_on_epoch_seconds=float(access_token.expires_on),
                )
            return self._current_token

    def _to_redis_token(
        self,
        *,
        token_value: str,
        expires_on_epoch_seconds: float,
    ) -> TokenInterface:
        parsed_token = JWToken(token_value)
        token_oid = self._object_id or parsed_token.try_get("oid") or parsed_token.try_get("sub")
        if not token_oid:
            msg = "Azure Entra token missing 'oid'. Set REDIS_ENTRA_OBJECT_ID explicitly."
            raise RuntimeError(msg)

        claims = {"oid": str(token_oid)}
        token_sub = parsed_token.try_get("sub")
        if token_sub:
            claims["sub"] = str(token_sub)

        return SimpleToken(
            value=token_value,
            expires_at_ms=expires_on_epoch_seconds * 1000,
            received_at_ms=time.time() * 1000,
            claims=claims,
        )

    def _dispatch_callback(self, callback, payload: Any) -> None:
        if callback is None:
            return

        try:
            result = callback(payload)
            if inspect.isawaitable(result):
                if self._callback_loop is None or not self._callback_loop.is_running():
                    msg = "Redis Entra re-auth callback requires an active event loop."
                    raise RuntimeError(msg)
                future = asyncio.run_coroutine_threadsafe(result, self._callback_loop)
                future.result(timeout=30)
        except Exception as exc:  # noqa: BLE001
            if callback is not self._on_error_callback and self._on_error_callback is not None:
                self._dispatch_callback(self._on_error_callback, exc)
