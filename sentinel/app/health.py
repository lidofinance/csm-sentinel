import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from sentinel.app.build_info import load_build_info

logger = logging.getLogger(__name__)

HEALTH_HEARTBEAT_INTERVAL_SECONDS = 5.0
LIVENESS_STALE_AFTER_SECONDS = 30.0
READINESS_PROGRESS_STALE_AFTER_SECONDS = 30.0 * 60.0
DEFAULT_HEALTHCHECK_HOST = "0.0.0.0"
DEFAULT_HEALTHCHECK_PORT = 8080


@dataclass(slots=True, frozen=True)
class HealthSnapshot:
    startup_complete: bool
    ready: bool
    live: bool
    polling_started: bool
    subscription_active: bool
    catchup_active: bool
    warmup_started: bool
    warmup_complete: bool
    warmup_error: str | None
    shutting_down: bool
    fatal_error: str | None
    heartbeat_age_seconds: float
    progress_age_seconds: float | None
    last_subscription_ok_age_seconds: float | None


class HealthState:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        now = self._clock()
        self._lock = threading.Lock()
        self._startup_complete = False
        self._polling_started = False
        self._subscription_active = False
        self._catchup_active = False
        self._warmup_started = False
        self._warmup_complete = False
        self._warmup_error: str | None = None
        self._shutting_down = False
        self._fatal_error: str | None = None
        self._last_heartbeat_at = now
        self._last_progress_at: float | None = None
        self._last_subscription_ok_at: float | None = None

    def mark_polling_started(self) -> None:
        with self._lock:
            self._polling_started = True
            self._last_heartbeat_at = self._clock()

    def mark_subscription_active(self) -> None:
        now = self._clock()
        with self._lock:
            self._subscription_active = True
            self._last_subscription_ok_at = now
            self._last_progress_at = now
            self._last_heartbeat_at = now

    def mark_subscription_inactive(self) -> None:
        with self._lock:
            self._subscription_active = False

    def mark_progress(self) -> None:
        now = self._clock()
        with self._lock:
            self._last_progress_at = now
            self._last_heartbeat_at = now

    def mark_catchup_started(self) -> None:
        with self._lock:
            self._catchup_active = True

    def mark_catchup_complete(self) -> None:
        now = self._clock()
        with self._lock:
            self._catchup_active = False
            self._last_progress_at = now
            self._last_heartbeat_at = now

    def mark_startup_complete(self) -> None:
        now = self._clock()
        with self._lock:
            self._startup_complete = True
            self._last_progress_at = now
            self._last_heartbeat_at = now

    def mark_warmup_started(self) -> None:
        now = self._clock()
        with self._lock:
            self._warmup_started = True
            self._warmup_complete = False
            self._warmup_error = None
            self._last_heartbeat_at = now

    def mark_warmup_complete(self) -> None:
        now = self._clock()
        with self._lock:
            self._warmup_started = True
            self._warmup_complete = True
            self._warmup_error = None
            self._last_progress_at = now
            self._last_heartbeat_at = now

    def mark_warmup_failed(self, exc: BaseException | str) -> None:
        message = str(exc)
        now = self._clock()
        with self._lock:
            self._warmup_started = True
            self._warmup_complete = True
            self._warmup_error = message or exc.__class__.__name__
            self._last_heartbeat_at = now

    def mark_shutdown_requested(self) -> None:
        with self._lock:
            self._shutting_down = True

    def mark_fatal_error(self, exc: BaseException | str) -> None:
        message = str(exc)
        with self._lock:
            self._fatal_error = message or exc.__class__.__name__

    def heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat_at = self._clock()

    async def heartbeat_loop(
        self, *, interval_seconds: float = HEALTH_HEARTBEAT_INTERVAL_SECONDS
    ) -> None:
        while True:
            self.heartbeat()
            await asyncio.sleep(interval_seconds)

    def snapshot(self) -> HealthSnapshot:
        now = self._clock()
        with self._lock:
            heartbeat_age = now - self._last_heartbeat_at
            progress_age = None if self._last_progress_at is None else now - self._last_progress_at
            subscription_age = (
                None
                if self._last_subscription_ok_at is None
                else now - self._last_subscription_ok_at
            )
            live = self._fatal_error is None and heartbeat_age < LIVENESS_STALE_AFTER_SECONDS
            ready = (
                self._startup_complete
                and self._polling_started
                and self._subscription_active
                and not self._catchup_active
                and not self._shutting_down
                and self._fatal_error is None
                and progress_age is not None
                and progress_age < READINESS_PROGRESS_STALE_AFTER_SECONDS
            )
            return HealthSnapshot(
                startup_complete=self._startup_complete,
                ready=ready,
                live=live,
                polling_started=self._polling_started,
                subscription_active=self._subscription_active,
                catchup_active=self._catchup_active,
                warmup_started=self._warmup_started,
                warmup_complete=self._warmup_complete,
                warmup_error=self._warmup_error,
                shutting_down=self._shutting_down,
                fatal_error=self._fatal_error,
                heartbeat_age_seconds=heartbeat_age,
                progress_age_seconds=progress_age,
                last_subscription_ok_age_seconds=subscription_age,
            )


class HealthServer:
    def __init__(
        self,
        state: HealthState,
        *,
        host: str = DEFAULT_HEALTHCHECK_HOST,
        port: int = DEFAULT_HEALTHCHECK_PORT,
        build_info: dict[str, str] | None = None,
    ) -> None:
        self._state = state
        self._build_info = build_info or load_build_info()
        self._server = ThreadingHTTPServer((host, port), self._build_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health-server", daemon=True
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread.start()
        logger.info("Health server listening on %s", self._server.server_address)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _build_handler(self):
        state = self._state
        build_info = self._build_info

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/build-info.json":
                    self._send_json(HTTPStatus.OK, build_info)
                    return

                snapshot = state.snapshot()
                if self.path == "/startup":
                    ok = snapshot.startup_complete
                elif self.path == "/ready":
                    ok = snapshot.ready
                elif self.path == "/live":
                    ok = snapshot.live
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return

                self._send_json(
                    HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "startup_complete": snapshot.startup_complete,
                        "ready": snapshot.ready,
                        "live": snapshot.live,
                        "polling_started": snapshot.polling_started,
                        "subscription_active": snapshot.subscription_active,
                        "catchup_active": snapshot.catchup_active,
                        "warmup_started": snapshot.warmup_started,
                        "warmup_complete": snapshot.warmup_complete,
                        "warmup_error": snapshot.warmup_error,
                        "shutting_down": snapshot.shutting_down,
                        "fatal_error": snapshot.fatal_error,
                        "heartbeat_age_seconds": round(snapshot.heartbeat_age_seconds, 3),
                        "progress_age_seconds": (
                            None
                            if snapshot.progress_age_seconds is None
                            else round(snapshot.progress_age_seconds, 3)
                        ),
                        "last_subscription_ok_age_seconds": (
                            None
                            if snapshot.last_subscription_ok_age_seconds is None
                            else round(snapshot.last_subscription_ok_age_seconds, 3)
                        ),
                    },
                )

            def _send_json(self, status: HTTPStatus, body: dict) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return

        return Handler
