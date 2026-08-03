import asyncio
import logging
import re
import time
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, TypeVar, cast
from urllib.parse import urlsplit

from eth_typing import URI
from web3 import AsyncWeb3, WebSocketProvider
from web3.exceptions import (
    BlockNotFound,
    PersistentConnectionError,
    ProviderConnectionError,
    TooManyRequests,
    TransactionNotFound,
    Web3RPCError,
)
from web3.types import RPCEndpoint, RPCResponse
from web3.manager import RequestManager
from web3.providers.async_base import AsyncJSONBaseProvider
from websockets.exceptions import WebSocketException

from sentinel.app.build_info import application_user_agent
from sentinel.metrics.rpc import NOOP_RPC_OBSERVER, RpcObserver

logger = logging.getLogger(__name__)
T = TypeVar("T")
DEFAULT_CONNECTION_RETRIES_PER_ENDPOINT = 5
_MAX_RPC_ERROR_MESSAGE_LENGTH = 240
_RPC_URL_PATTERN = re.compile(r"\b(?:https?|wss?)://[^\s]+", re.IGNORECASE)
_RPC_TRANSPORT_EXCEPTIONS = (
    PersistentConnectionError,
    ProviderConnectionError,
    WebSocketException,
    OSError,
)
_RPC_ENDPOINT_FAILURES = (*_RPC_TRANSPORT_EXCEPTIONS, TooManyRequests, Web3RPCError)
_RPC_SEMANTIC_RESPONSES = (BlockNotFound, TransactionNotFound)


class RpcError(RuntimeError):
    """Base class for Sentinel-owned RPC failures."""


class RpcAvailabilityError(RpcError):
    """An infrastructure failure that the application can recover from."""


class RpcConfigurationError(RpcError):
    """A fatal RPC configuration or network-integrity failure."""


class RpcEndpointsUnavailable(RpcAvailabilityError):
    pass


class RpcChainMismatch(RpcConfigurationError):
    pass


@dataclass(frozen=True, slots=True)
class RpcEndpoint:
    index: int
    uri: str

    @property
    def metric_label(self) -> str:
        return urlsplit(self.uri).hostname or f"rpc-{self.index + 1}"

    @property
    def label(self) -> str:
        hostname = urlsplit(self.uri).hostname
        return f"rpc-{self.index + 1}" if hostname is None else f"rpc-{self.index + 1} ({hostname})"


class RpcFailureKind(str, Enum):
    TRANSPORT = "transport"
    RPC_REJECTED = "rpc_rejected"


@dataclass(frozen=True, slots=True)
class RpcFailureSummary:
    endpoint_index: int | None
    endpoint_label: str | None
    kind: RpcFailureKind
    rpc_code: int | None = None


class RpcRequestRejectedByAllProviders(RpcAvailabilityError):
    def __init__(
        self,
        method: RPCEndpoint,
        failures: tuple[RpcFailureSummary, ...],
    ) -> None:
        self.method = method
        self.failures = failures
        summary = ", ".join(
            f"{failure.endpoint_label}: {failure.kind.value}" for failure in failures
        )
        super().__init__(f"All RPC endpoints rejected {method}: {summary}")


class RpcSubscriptionReconnectRequired(RpcAvailabilityError):
    def __init__(
        self,
        method: RPCEndpoint,
        failure: RpcFailureSummary,
    ) -> None:
        self.method = method
        self.failure = failure
        endpoint = failure.endpoint_label or "active endpoint"
        super().__init__(
            f"Subscription RPC endpoint {endpoint} requires reconnect for {method}: "
            f"{failure.kind.value}"
        )

    @classmethod
    def listener_stopped(cls) -> "RpcSubscriptionReconnectRequired":
        return cls(
            RPCEndpoint("subscription_stream"),
            RpcFailureSummary(
                endpoint_index=None,
                endpoint_label=None,
                kind=RpcFailureKind.TRANSPORT,
            ),
        )


def _normalize_subscription_exception(exc: BaseException) -> RpcAvailabilityError | None:
    """Translate provider-library failures at the subscription boundary."""

    if isinstance(exc, RpcAvailabilityError):
        return exc
    if isinstance(exc, _RPC_SEMANTIC_RESPONSES):
        return None
    if isinstance(exc, _RPC_ENDPOINT_FAILURES):
        return RpcSubscriptionReconnectRequired(
            RPCEndpoint("subscription_stream"),
            _failure_summary(None, exc),
        )
    return None


def _failure_summary(
    endpoint: RpcEndpoint | None,
    exc: BaseException,
) -> RpcFailureSummary:
    rpc_code: int | None = None
    if isinstance(exc, (TooManyRequests, Web3RPCError)):
        kind = RpcFailureKind.RPC_REJECTED
        if isinstance(exc, Web3RPCError):
            response = exc.rpc_response
            error = response.get("error") if isinstance(response, dict) else None
            if isinstance(error, dict) and isinstance(error.get("code"), int):
                rpc_code = error["code"]
    else:
        kind = RpcFailureKind.TRANSPORT
    return RpcFailureSummary(
        endpoint_index=None if endpoint is None else endpoint.index,
        endpoint_label=None if endpoint is None else endpoint.label,
        kind=kind,
        rpc_code=rpc_code,
    )


def _safe_rpc_error_message(exc: BaseException) -> str:
    message: object | None = None
    if isinstance(exc, Web3RPCError):
        response = exc.rpc_response
        error = response.get("error") if isinstance(response, dict) else None
        if isinstance(error, dict):
            message = error.get("message")
    elif isinstance(exc, TooManyRequests):
        message = str(exc)

    if not isinstance(message, str) or not message.strip():
        return exc.__class__.__name__

    normalized = " ".join(message.split())

    def redact_url(match: re.Match[str]) -> str:
        hostname = urlsplit(match.group(0)).hostname
        return hostname or "RPC URL"

    redacted = _RPC_URL_PATTERN.sub(redact_url, normalized)
    if len(redacted) <= _MAX_RPC_ERROR_MESSAGE_LENGTH:
        return redacted
    return f"{redacted[: _MAX_RPC_ERROR_MESSAGE_LENGTH - 3]}..."


class _RpcOperationFailed(Exception):
    def __init__(self, failure: RpcFailureSummary, log_message: str) -> None:
        self.failure = failure
        self.log_message = log_message
        super().__init__(failure.kind.value)


async def _invoke_rpc_operation(
    operation: Callable[[], Awaitable[T]],
    endpoint: RpcEndpoint,
) -> T:
    """Normalize provider-library failures without intercepting domain errors."""

    try:
        return await operation()
    except _RPC_SEMANTIC_RESPONSES:
        raise
    except _RPC_ENDPOINT_FAILURES as exc:
        raise _RpcOperationFailed(
            _failure_summary(endpoint, exc),
            _safe_rpc_error_message(exc),
        ) from None


@dataclass(slots=True)
class _EndpointState:
    cooldown_until: float = 0.0
    subscription_failure_count: int = 0
    last_subscription_failure_at: float | None = None


class RpcEndpointPool:
    def __init__(
        self,
        endpoint_uris: tuple[str, ...],
        *,
        chain_id: int | None = None,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not endpoint_uris:
            raise ValueError("At least one RPC endpoint must be configured")
        if len(set(endpoint_uris)) != len(endpoint_uris):
            raise ValueError("RPC endpoints must be unique")

        self.endpoints = tuple(
            RpcEndpoint(index=index, uri=uri) for index, uri in enumerate(endpoint_uris)
        )
        self.chain_id = chain_id
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock or time.monotonic
        self._states = [_EndpointState() for _ in self.endpoints]
        self._preferred_index = 0
        self._lock = asyncio.Lock()

    async def candidates(
        self,
        *,
        excluded_indices: frozenset[int] = frozenset(),
    ) -> tuple[tuple[RpcEndpoint, ...], float | None]:
        async with self._lock:
            now = self._clock()
            eligible = [
                (endpoint, state)
                for endpoint, state in zip(self.endpoints, self._states)
                if endpoint.index not in excluded_indices
            ]
            if not eligible:
                return (), None

            available = [endpoint for endpoint, state in eligible if state.cooldown_until <= now]
            if available:
                available.sort(key=lambda endpoint: endpoint.index != self._preferred_index)
                return tuple(available), 0.0

            cooldowns = [state.cooldown_until - now for _, state in eligible]
            return (), max(min(cooldowns), 0.0)

    async def mark_failed(self, endpoint: RpcEndpoint) -> None:
        async with self._lock:
            self._states[endpoint.index].cooldown_until = self._clock() + self.cooldown_seconds

    async def mark_success(self, endpoint: RpcEndpoint) -> None:
        async with self._lock:
            self._states[endpoint.index].cooldown_until = 0.0
            self._preferred_index = endpoint.index

    async def record_subscription_transport_failure(self, endpoint: RpcEndpoint) -> bool:
        """Return whether repeated stream failure should move the endpoint to cooldown."""

        async with self._lock:
            now = self._clock()
            state = self._states[endpoint.index]
            last_failure = state.last_subscription_failure_at
            if last_failure is None or now - last_failure > self.cooldown_seconds:
                state.subscription_failure_count = 0
            state.subscription_failure_count += 1
            state.last_subscription_failure_at = now
            if state.subscription_failure_count < 2:
                return False
            state.subscription_failure_count = 0
            state.cooldown_until = now + self.cooldown_seconds
            return True

    async def accept_chain_id(self, endpoint: RpcEndpoint, chain_id: int) -> None:
        async with self._lock:
            if self.chain_id is None:
                self.chain_id = chain_id
            elif chain_id != self.chain_id:
                raise RpcChainMismatch(
                    f"{endpoint.label} has chain ID {chain_id}, but the RPC pool uses "
                    f"chain ID {self.chain_id}"
                )


class FallbackConnectionBase:
    """Shared endpoint selection and connection lifecycle for fallback providers."""

    def __init__(
        self,
        pool: RpcEndpointPool,
        *,
        role: str,
        max_connection_rounds: int,
        retry_interval_seconds: float,
        observer: RpcObserver,
    ) -> None:
        if max_connection_rounds == 0 or max_connection_rounds < -1:
            raise ValueError("max_connection_rounds must be -1 or a positive integer")
        self.pool = pool
        self.role = role
        self.max_connection_rounds = max_connection_rounds
        self.retry_interval_seconds = retry_interval_seconds
        self.active_endpoint: RpcEndpoint | None = None
        self.connection_generation = 0
        self._observer = observer
        self._connect_lock = asyncio.Lock()
        self._failover_lock = asyncio.Lock()

    async def validate_endpoint_chain_ids(self) -> None:
        unavailable_endpoints: list[str] = []
        async with self._connect_lock:
            for endpoint in self.pool.endpoints:
                try:
                    await self._open_endpoint(endpoint)
                    chain_id = await self._read_chain_id()
                    await self.pool.accept_chain_id(endpoint, chain_id)
                except asyncio.CancelledError:
                    raise
                except RpcChainMismatch:
                    logger.error(
                        "%s endpoint %s belongs to a different chain",
                        self.role,
                        endpoint.label,
                    )
                    raise
                except _RPC_ENDPOINT_FAILURES as exc:
                    self._observer.endpoint_failed(
                        self.role,
                        endpoint.metric_label,
                        _failure_summary(endpoint, exc).kind.value,
                    )
                    logger.error(
                        "%s endpoint %s is unavailable during startup validation",
                        self.role,
                        endpoint.label,
                    )
                    await self.pool.mark_failed(endpoint)
                    unavailable_endpoints.append(endpoint.label)
                else:
                    logger.info(
                        "%s endpoint %s validated with chain ID %s",
                        self.role,
                        endpoint.label,
                        chain_id,
                    )
                finally:
                    await self._close_safely()

        if unavailable_endpoints:
            labels = ", ".join(unavailable_endpoints)
            raise RpcEndpointsUnavailable(
                f"RPC endpoints unavailable during startup validation: {labels}"
            )

    async def _connect_with_fallback(
        self,
        *,
        excluded_indices: frozenset[int] = frozenset(),
        max_rounds: int | None = None,
    ) -> None:
        connection_rounds = self.max_connection_rounds if max_rounds is None else max_rounds
        if connection_rounds == 0 or connection_rounds < -1:
            raise ValueError("max_rounds must be -1 or a positive integer")

        async with self._connect_lock:
            if self.active_endpoint is not None:
                if self.active_endpoint.index in excluded_indices:
                    self.active_endpoint = None
                    self._observer.endpoint_disconnected(self.role)
                    await self._close_safely()
                elif await self.is_connected():
                    return

            completed_rounds = 0
            while connection_rounds == -1 or completed_rounds < connection_rounds:
                candidates, wait_seconds = await self.pool.candidates(
                    excluded_indices=excluded_indices
                )
                if not candidates:
                    if wait_seconds is None:
                        raise RpcEndpointsUnavailable(
                            f"No untried RPC endpoints remain for {self.role}"
                        )
                    await asyncio.sleep(max(wait_seconds, self.retry_interval_seconds))
                    continue

                for endpoint in candidates:
                    try:
                        await self._open_endpoint(endpoint)
                    except asyncio.CancelledError:
                        raise
                    except _RPC_ENDPOINT_FAILURES as exc:
                        self._observer.endpoint_failed(
                            self.role,
                            endpoint.metric_label,
                            _failure_summary(endpoint, exc).kind.value,
                        )
                        logger.warning(
                            "%s endpoint %s is unavailable",
                            self.role,
                            endpoint.label,
                        )
                        await self._close_safely()
                        await self.pool.mark_failed(endpoint)
                        continue

                    self.active_endpoint = endpoint
                    self.connection_generation += 1
                    await self.pool.mark_success(endpoint)
                    self._observer.endpoint_connected(self.role, endpoint.metric_label)
                    logger.info(
                        "%s connected through %s (generation %s)",
                        self.role,
                        endpoint.label,
                        self.connection_generation,
                    )
                    return

                completed_rounds += 1
                if connection_rounds != -1 and completed_rounds >= connection_rounds:
                    break
                await asyncio.sleep(self.retry_interval_seconds)

            raise RpcEndpointsUnavailable(f"All RPC endpoints are unavailable for {self.role}")

    async def _disconnect_with_fallback(self) -> None:
        async with self._connect_lock:
            self.active_endpoint = None
            self._observer.endpoint_disconnected(self.role)
            try:
                await self._close_endpoint()
            except _RPC_ENDPOINT_FAILURES:
                raise RpcEndpointsUnavailable(
                    f"Failed to disconnect active {self.role} RPC endpoint"
                ) from None

    async def _invalidate_endpoint(
        self,
        endpoint: RpcEndpoint,
        generation: int,
        *,
        cooldown: bool,
        log_connection_loss: bool = True,
    ) -> None:
        async with self._failover_lock:
            if self.active_endpoint == endpoint and self.connection_generation == generation:
                if cooldown:
                    await self.pool.mark_failed(endpoint)
                elif log_connection_loss:
                    logger.warning(
                        "%s endpoint %s connection lost; retrying before fallback",
                        self.role,
                        endpoint.label,
                    )
                self.active_endpoint = None
                self._observer.endpoint_disconnected(self.role)
                await self._close_safely()

    async def _close_safely(self) -> None:
        with suppress(*_RPC_ENDPOINT_FAILURES):
            await self._close_endpoint()

    async def _open_endpoint(self, endpoint: RpcEndpoint) -> None:
        raise NotImplementedError

    async def _read_chain_id(self) -> int:
        raise NotImplementedError

    async def _close_endpoint(self) -> None:
        raise NotImplementedError

    async def is_connected(self, show_traceback: bool = False) -> bool:
        raise NotImplementedError


class FallbackRequestProvider(AsyncJSONBaseProvider, FallbackConnectionBase):
    """Non-persistent Web3 facade over independent persistent RPC transports."""

    def __init__(
        self,
        pool: RpcEndpointPool,
        *,
        role: str,
        max_connection_rounds: int = -1,
        retry_interval_seconds: float = 1.0,
        max_connection_retries: int = DEFAULT_CONNECTION_RETRIES_PER_ENDPOINT,
        observer: RpcObserver = NOOP_RPC_OBSERVER,
    ) -> None:
        if max_connection_retries < 1:
            raise ValueError("max_connection_retries must be a positive integer")

        super().__init__()
        FallbackConnectionBase.__init__(
            self,
            pool,
            role=role,
            max_connection_rounds=max_connection_rounds,
            retry_interval_seconds=retry_interval_seconds,
            observer=observer,
        )
        self.observer = observer
        self._active_transport: WebSocketProvider | None = None
        self._transports = tuple(
            WebSocketProvider(
                endpoint.uri,
                max_connection_retries=max_connection_retries,
                websocket_kwargs={"user_agent_header": application_user_agent()},
            )
            for endpoint in pool.endpoints
        )
        self._transport_web3s = tuple(
            AsyncWeb3(transport, middleware=[]) for transport in self._transports
        )

    @property
    def active_endpoint_label(self) -> str:
        return "not-connected" if self.active_endpoint is None else self.active_endpoint.label

    async def connect(self) -> None:
        await self._connect_with_fallback()

    async def disconnect(self) -> None:
        await self._disconnect_with_fallback()

    async def is_connected(self, show_traceback: bool = False) -> bool:
        del show_traceback
        if self._active_transport is None:
            return False
        return await self._active_transport.is_connected()

    async def make_request(self, method: RPCEndpoint, params: Any) -> RPCResponse:
        await self.connect()
        endpoint = self.active_endpoint
        assert endpoint is not None
        transport_w3 = self._transport_web3s[endpoint.index]
        try:
            result = await transport_w3.manager.socket_request(method, params)
        except Web3RPCError as exc:
            return cast(RPCResponse, exc.rpc_response)
        return RPCResponse(jsonrpc="2.0", id=0, result=result)

    async def execute_request(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        method: RPCEndpoint,
    ) -> T:
        rejected_indices: set[int] = set()
        request_failures: list[RpcFailureSummary] = []
        transport_retried_indices: set[int] = set()

        while True:
            await self._connect_with_fallback(
                excluded_indices=frozenset(rejected_indices),
                max_rounds=1 if rejected_indices else None,
            )
            endpoint = self.active_endpoint
            assert endpoint is not None
            generation = self.connection_generation
            try:
                return await _invoke_rpc_operation(operation, endpoint)
            except _RpcOperationFailed as exc:
                failure = exc.failure
                self.observer.endpoint_failed(self.role, endpoint.metric_label, failure.kind.value)
                if failure.kind is RpcFailureKind.TRANSPORT:
                    already_retried = endpoint.index in transport_retried_indices
                    transport_retried_indices.add(endpoint.index)
                    if already_retried:
                        rejected_indices.add(endpoint.index)
                    await self._invalidate_endpoint(
                        endpoint,
                        generation,
                        cooldown=already_retried,
                    )
                    if len(rejected_indices) == len(self.pool.endpoints):
                        raise RpcEndpointsUnavailable(
                            f"All RPC endpoints lost connection while executing {method}"
                        ) from None
                    continue

                rejected_indices.add(endpoint.index)
                request_failures.append(failure)
                logger.warning(
                    "%s endpoint %s rejected %s (RPC code %s): %s",
                    self.role,
                    endpoint.label,
                    method,
                    failure.rpc_code,
                    exc.log_message,
                )
                await self._invalidate_endpoint(
                    endpoint,
                    generation,
                    cooldown=False,
                    log_connection_loss=False,
                )
                if len(rejected_indices) == len(self.pool.endpoints):
                    raise RpcRequestRejectedByAllProviders(
                        method,
                        tuple(request_failures),
                    ) from None

    async def _open_endpoint(self, endpoint: RpcEndpoint) -> None:
        self._active_transport = self._transports[endpoint.index]
        await self._active_transport.connect()

    async def _read_chain_id(self) -> int:
        endpoint = self.active_endpoint
        if endpoint is None:
            transport = self._active_transport
            assert transport is not None
            endpoint_index = self._transports.index(transport)
        else:
            endpoint_index = endpoint.index
        return await self._read_chain_id_from_w3(self._transport_web3s[endpoint_index])

    async def _close_endpoint(self) -> None:
        transport = self._active_transport
        self._active_transport = None
        if transport is not None:
            await transport.disconnect()

    @staticmethod
    async def _read_chain_id_from_w3(transport_w3: AsyncWeb3) -> int:
        result = await transport_w3.manager.socket_request(
            RPCEndpoint("eth_chainId"),
            [],
        )
        if isinstance(result, int):
            return result
        if isinstance(result, str):
            return int(result, 0)
        raise ProviderConnectionError("RPC endpoint returned an invalid eth_chainId response")


class FallbackSubscriptionProvider(WebSocketProvider, FallbackConnectionBase):
    def __init__(
        self,
        pool: RpcEndpointPool,
        *,
        role: str,
        max_connection_rounds: int = -1,
        retry_interval_seconds: float = 1.0,
        observer: RpcObserver = NOOP_RPC_OBSERVER,
        **kwargs,
    ) -> None:
        max_connection_retries = kwargs.pop(
            "max_connection_retries", DEFAULT_CONNECTION_RETRIES_PER_ENDPOINT
        )
        if max_connection_retries < 1:
            raise ValueError("max_connection_retries must be a positive integer")
        self.observer = observer
        websocket_kwargs = dict(kwargs.pop("websocket_kwargs", {}))
        websocket_kwargs["user_agent_header"] = application_user_agent()
        super().__init__(
            endpoint_uri=pool.endpoints[0].uri,
            max_connection_retries=max_connection_retries,
            websocket_kwargs=websocket_kwargs,
            **kwargs,
        )
        FallbackConnectionBase.__init__(
            self,
            pool,
            role=role,
            max_connection_rounds=max_connection_rounds,
            retry_interval_seconds=retry_interval_seconds,
            observer=observer,
        )

    def __str__(self) -> str:
        return f"Fallback WebSocket connection ({self.role}, {self.active_endpoint_label})"

    def _handle_listener_task_exceptions(self) -> None:
        try:
            super()._handle_listener_task_exceptions()
        except BaseException as exc:
            normalized = _normalize_subscription_exception(exc)
            if normalized is None:
                raise
            logger.warning(
                "%s endpoint %s subscription listener failed: %s",
                self.role,
                self.active_endpoint_label,
                exc.__class__.__name__,
            )
            raise normalized from None

    @property
    def active_endpoint_label(self) -> str:
        return "not-connected" if self.active_endpoint is None else self.active_endpoint.label

    def get_endpoint_uri_or_ipc_path(self) -> str:
        return f"{self.role}:{self.active_endpoint_label}"

    async def connect(self) -> None:
        await self._connect_with_fallback()

    async def execute_subscription(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        method: RPCEndpoint,
    ) -> T:
        await self.connect()
        endpoint = self.active_endpoint
        assert endpoint is not None
        generation = self.connection_generation
        try:
            return await _invoke_rpc_operation(operation, endpoint)
        except _RpcOperationFailed as exc:
            failure = exc.failure
            self.observer.endpoint_failed(self.role, endpoint.metric_label, failure.kind.value)
            self.observer.persistent_request_failed(self.role, str(method), failure.kind.value)
            rejected = failure.kind is RpcFailureKind.RPC_REJECTED
            if rejected:
                logger.warning(
                    "%s endpoint %s rejected %s (RPC code %s): %s",
                    self.role,
                    endpoint.label,
                    method,
                    failure.rpc_code,
                    exc.log_message,
                )
            cooldown = rejected or await self.pool.record_subscription_transport_failure(endpoint)
            await self._invalidate_endpoint(
                endpoint,
                generation,
                cooldown=cooldown,
                log_connection_loss=not cooldown,
            )
            raise RpcSubscriptionReconnectRequired(method, failure) from None

    async def _open_endpoint(self, endpoint: RpcEndpoint) -> None:
        self.endpoint_uri = URI(endpoint.uri)
        await super().connect()

    async def _close_endpoint(self) -> None:
        await super().disconnect()

    async def disconnect(self) -> None:
        await self._disconnect_with_fallback()

    async def _read_chain_id(self) -> int:
        response = await super().make_request(RPCEndpoint("eth_chainId"), [])
        result = response.get("result")
        if isinstance(result, int):
            return result
        if isinstance(result, str):
            return int(result, 0)
        raise ProviderConnectionError("RPC endpoint returned an invalid eth_chainId response")


class FallbackRequestManager(RequestManager):
    async def coro_request(
        self,
        method: RPCEndpoint | Callable[..., RPCEndpoint],
        params: Any,
        error_formatters: Callable[..., Any] | None = None,
        null_result_formatters: Callable[..., Any] | None = None,
    ) -> Any:
        provider = self.provider
        if not isinstance(provider, FallbackRequestProvider):
            return await super().coro_request(
                method,
                params,
                error_formatters,
                null_result_formatters,
            )
        rpc_method = cast(RPCEndpoint, method)
        return await provider.execute_request(
            lambda: super(FallbackRequestManager, self).coro_request(
                method,
                params,
                error_formatters,
                null_result_formatters,
            ),
            method=rpc_method,
        )

    async def socket_request(
        self,
        method: RPCEndpoint,
        params: Any,
        response_formatters: Any = None,
    ) -> RPCResponse:
        provider = self.provider
        if not isinstance(provider, FallbackSubscriptionProvider):
            return await super().socket_request(method, params, response_formatters)
        return await provider.execute_subscription(
            lambda: super(FallbackRequestManager, self).socket_request(
                method,
                params,
                response_formatters,
            ),
            method=method,
        )


class FallbackAsyncWeb3(AsyncWeb3):
    RequestManager = FallbackRequestManager
