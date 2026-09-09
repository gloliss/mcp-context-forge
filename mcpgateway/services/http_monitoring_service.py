# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/http_monitoring_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Primary-worker HTTP service health monitoring (PR3, design §19).

Mirrors ``grpc_monitoring_service`` for the HTTP registry: one SSRF-checked
GET per check, bounded concurrency, and the same
healthy/degraded/unhealthy state machine with a failure threshold.  Unlike
the gRPC monitor there is deliberately **no sample or metrics table** — PR3
persists only the rolling health state on ``http_services`` (§20 exposes no
health-history endpoint).
"""

# Standard
import asyncio
from datetime import datetime, timezone
import random
from typing import Any, Optional

# Third-Party
from sqlalchemy import select

# First-Party
from mcpgateway.common.validators import SecurityValidator
from mcpgateway.config import settings
from mcpgateway.db import fresh_db_session
from mcpgateway.db import HttpService as DbHttpService
from mcpgateway.services.http_client_service import get_isolated_http_client
from mcpgateway.services.logging_service import LoggingService
from mcpgateway.utils.primary_worker import is_primary_worker

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

# Maximum concurrent in-flight health checks.
_DEFAULT_MAX_CONCURRENT_CHECKS = 10


class HttpMonitoringService:
    """HTTP health monitor with concurrent, primary-worker-gated checks."""

    def __init__(self) -> None:
        """Initialize the singleton monitor lifecycle state."""
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._max_concurrent = _DEFAULT_MAX_CONCURRENT_CHECKS

    async def start(self) -> None:
        """Start one monitor loop per process; only the primary performs checks."""
        if not settings.mcpgateway_http_health_enabled:
            return
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run())

    async def shutdown(self) -> None:
        """Stop the monitor loop."""
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @classmethod
    async def _perform_request(cls, snapshot: DbHttpService) -> tuple[bool, Optional[int], Optional[str], float]:
        """Run one SSRF-checked health GET against the service base URL.

        Args:
            snapshot: Detached service snapshot carrying ``base_url`` and
                ``health_check_timeout``.

        Returns:
            Tuple of (response_received, status_code, error, latency_ms).
        """
        started = asyncio.get_event_loop().time()
        try:
            await asyncio.to_thread(SecurityValidator.validate_url, snapshot.base_url, "HTTP service base URL")
            async with get_isolated_http_client(timeout=snapshot.health_check_timeout, follow_redirects=False) as client:
                response = await client.get(snapshot.base_url)
            latency = (asyncio.get_event_loop().time() - started) * 1000
            return True, response.status_code, None, latency
        except Exception as exc:  # pylint: disable=broad-except
            latency = (asyncio.get_event_loop().time() - started) * 1000
            return False, None, str(exc)[:1000], latency

    @classmethod
    async def check_service(cls, service_id: str) -> dict[str, Any]:
        """Check one service and persist its rolling health state.

        Args:
            service_id: The service to check.

        Returns:
            The current health payload (never raises: transport failures are
            recorded as health state, matching the gRPC monitor).
        """
        with fresh_db_session() as read_db:
            service = read_db.get(DbHttpService, service_id)
            if service is None:
                return {"status": "missing"}
            snapshot = DbHttpService(
                id=service.id,
                base_url=service.base_url,
                health_check_timeout=service.health_check_timeout,
            )

        received, status_code, error, latency_ms = await cls._perform_request(snapshot)
        healthy = received and status_code is not None and 200 <= status_code < 400

        with fresh_db_session() as write_db:
            service = write_db.get(DbHttpService, service_id)
            if service is None:
                return {"status": "missing"}
            now_utc = datetime.now(timezone.utc)
            service.last_health_check = now_utc
            if healthy:
                service.consecutive_failures = 0
                service.health_status = "healthy"
                service.last_health_error = None
                service.last_health_success = now_utc
                service.reachable = True
            else:
                service.consecutive_failures += 1
                service.health_status = "unhealthy" if service.consecutive_failures >= service.health_failure_threshold else "degraded"
                service.last_health_error = error or (f"HTTP {status_code}" if status_code is not None else "Connection failed")
                # A response of any status proves the transport is reachable;
                # only connection-level failures flip reachability.
                service.reachable = received
            status = service.health_status
            last_health_success = service.last_health_success.isoformat() if service.last_health_success else None
            service_slug = service.slug

        return {
            "status": status,
            "healthy": healthy,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "error": error,
            "last_health_success": last_health_success,
            "service": service_slug,
        }

    async def _run(self) -> None:
        """Schedule jittered, concurrent checks from the primary worker.

        Services due for a check are gathered in parallel, bounded by
        ``_max_concurrent`` to avoid overwhelming the process or upstreams.
        """
        while not self._stopping.is_set():
            try:
                if is_primary_worker():
                    now = datetime.now(timezone.utc)
                    with fresh_db_session() as db:
                        services = list(
                            db.execute(
                                select(DbHttpService.id, DbHttpService.health_check_interval, DbHttpService.last_health_check).where(
                                    DbHttpService.enabled.is_(True), DbHttpService.health_check_enabled.is_(True)
                                )
                            ).all()
                        )
                    due: list[str] = []
                    for service_id, interval, last_check in services:
                        last_check_utc = last_check.replace(tzinfo=timezone.utc) if last_check and last_check.tzinfo is None else last_check
                        jittered = max(10, int(interval * random.uniform(0.9, 1.1)))  # nosec B311
                        if last_check_utc is None or (now - last_check_utc).total_seconds() >= jittered:
                            due.append(service_id)
                    if due:
                        sem = asyncio.Semaphore(max(1, self._max_concurrent))

                        async def _check_one(sid: str) -> None:
                            """Run one service check inside the semaphore."""
                            async with sem:
                                try:
                                    await self.check_service(sid)
                                except Exception:  # pylint: disable=broad-except
                                    logger.exception("Health check failed for HTTP service %s", sid)

                        await asyncio.gather(*(_check_one(sid) for sid in due), return_exceptions=True)
                await asyncio.wait_for(self._stopping.wait(), timeout=5)
            except asyncio.TimeoutError:
                continue
            except Exception:  # pylint: disable=broad-except
                logger.exception("HTTP health monitor loop error")
                await asyncio.sleep(5)


_monitor = HttpMonitoringService()


def get_http_monitoring_service() -> HttpMonitoringService:
    """Return the process singleton monitor."""
    return _monitor
