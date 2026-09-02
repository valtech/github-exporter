from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus

from aiohttp import web
from githubkit import GitHub
from githubkit.exception import RequestError, RequestFailed, RequestTimeout
from githubkit_schemas.ghec_v2026_03_10.models import (
    AdvancedSecurityActiveCommitters,
    EnterprisesEnterpriseCopilotBillingSeatsGetResponse200,
    GetConsumedLicenses,
)
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric

LOGGER = logging.getLogger(__name__)


class GitHubApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SeatMetric:
    feature: str
    used: float
    total: float | None
    available: float | None


CollectorFn = Callable[[], Awaitable[SeatMetric]]


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    collector: CollectorFn


@dataclass(frozen=True)
class ScrapeData:
    enterprise: str
    scraped_at: datetime
    seat_metrics: tuple[SeatMetric, ...]


class GitHubClient:
    def __init__(self, api_url: str, token: str, timeout_seconds: float) -> None:
        self._api = GitHub(
            auth=token,
            base_url=api_url,
            timeout=timeout_seconds,
            user_agent="github-enterprise-exporter",
        )
        self._rest = self._api.rest("ghec-2026-03-10")

    async def get_consumed_licenses(self, enterprise: str) -> GetConsumedLicenses:
        endpoint = f"/enterprises/{enterprise}/consumed-licenses"
        try:
            response = await self._rest.enterprise_admin.async_get_consumed_licenses(
                enterprise, per_page=1
            )
            return response.parsed_data
        except RequestFailed as exc:
            raise GitHubApiError(
                f"GitHub API request failed for {endpoint}: HTTP {exc.response.status_code}",
                status_code=exc.response.status_code,
            ) from exc
        except RequestTimeout as exc:
            raise GitHubApiError(f"GitHub API request timed out for {endpoint}: {exc!r}") from exc
        except RequestError as exc:
            raise GitHubApiError(f"GitHub API request failed for {endpoint}: {exc!r}") from exc

    async def get_copilot_seats(
        self, enterprise: str
    ) -> EnterprisesEnterpriseCopilotBillingSeatsGetResponse200:
        endpoint = f"/enterprises/{enterprise}/copilot/billing/seats"
        try:
            response = await self._rest.copilot.async_list_copilot_seats_for_enterprise(
                enterprise, per_page=1
            )
            return response.parsed_data
        except RequestFailed as exc:
            raise GitHubApiError(
                f"GitHub API request failed for {endpoint}: HTTP {exc.response.status_code}",
                status_code=exc.response.status_code,
            ) from exc
        except RequestTimeout as exc:
            raise GitHubApiError(f"GitHub API request timed out for {endpoint}: {exc!r}") from exc
        except RequestError as exc:
            raise GitHubApiError(f"GitHub API request failed for {endpoint}: {exc!r}") from exc

    async def get_advanced_security_usage(
        self, enterprise: str
    ) -> AdvancedSecurityActiveCommitters:
        endpoint = f"/enterprises/{enterprise}/settings/billing/advanced-security"
        try:
            response = await self._rest.billing.async_get_github_advanced_security_billing_ghe(
                enterprise, per_page=1
            )
            return response.parsed_data
        except RequestFailed as exc:
            raise GitHubApiError(
                f"GitHub API request failed for {endpoint}: HTTP {exc.response.status_code}",
                status_code=exc.response.status_code,
            ) from exc
        except RequestTimeout as exc:
            raise GitHubApiError(f"GitHub API request timed out for {endpoint}: {exc!r}") from exc
        except RequestError as exc:
            raise GitHubApiError(f"GitHub API request failed for {endpoint}: {exc!r}") from exc


class EnterpriseCollector:
    def __init__(self, client: GitHubClient, enterprise: str) -> None:
        self._client = client
        self._enterprise = enterprise
        self._features: tuple[FeatureSpec, ...] = (
            FeatureSpec(
                name="ghec",
                collector=self._collect_ghec_licenses,
            ),
            FeatureSpec(
                name="copilot",
                collector=self._collect_copilot,
            ),
            FeatureSpec(
                name="advanced_security",
                collector=self._collect_advanced_security,
            ),
        )

    async def collect(self) -> ScrapeData:
        seat_metrics = await asyncio.gather(*(spec.collector() for spec in self._features))
        return ScrapeData(
            enterprise=self._enterprise,
            scraped_at=datetime.now(tz=UTC),
            seat_metrics=tuple(seat_metrics),
        )

    async def _collect_ghec_licenses(self) -> SeatMetric:
        payload = await self._client.get_consumed_licenses(self._enterprise)
        used = _require_int(payload.total_seats_consumed, "total_seats_consumed", "ghec")
        total = _optional_int(payload.total_seats_purchased)
        available = (total - used) if total is not None else None
        return SeatMetric(feature="ghec", used=used, total=total, available=available)

    async def _collect_copilot(self) -> SeatMetric:
        payload = await self._client.get_copilot_seats(self._enterprise)
        used = _require_int(payload.total_seats, "total_seats", "copilot")
        return SeatMetric(feature="copilot", used=used, total=None, available=None)

    async def _collect_advanced_security(self) -> SeatMetric:
        payload = await self._client.get_advanced_security_usage(self._enterprise)
        used = _require_int(
            payload.total_advanced_security_committers,
            "total_advanced_security_committers",
            "advanced_security",
        )
        total = _optional_int(payload.purchased_advanced_security_committers)
        available = (total - used) if total is not None else None
        return SeatMetric(feature="advanced_security", used=used, total=total, available=available)


class StaticPrometheusCollector:
    def __init__(self, scrape_data: ScrapeData) -> None:
        self._scrape_data = scrape_data

    def collect(self) -> Iterable[Metric]:
        seat_used = GaugeMetricFamily(
            "github_enterprise_license_seats_used",
            "Used seats or active users by feature.",
            labels=["enterprise", "feature"],
        )
        seat_total = GaugeMetricFamily(
            "github_enterprise_license_seats_total",
            "Total seats by feature where available.",
            labels=["enterprise", "feature"],
        )
        seat_available = GaugeMetricFamily(
            "github_enterprise_license_seats_available",
            "Available seats by feature where available.",
            labels=["enterprise", "feature"],
        )
        scrape_timestamp = GaugeMetricFamily(
            "github_exporter_last_scrape_timestamp_seconds",
            "Unix timestamp of last successful scrape.",
        )

        enterprise = self._scrape_data.enterprise
        for metric in self._scrape_data.seat_metrics:
            labels = [enterprise, metric.feature]
            seat_used.add_metric(labels, metric.used)
            if metric.total is not None:
                seat_total.add_metric(labels, metric.total)
            if metric.available is not None:
                seat_available.add_metric(labels, metric.available)

        scrape_timestamp.add_metric([], self._scrape_data.scraped_at.timestamp())

        return [seat_used, seat_total, seat_available, scrape_timestamp]


class ExportService:
    def __init__(self, collector: EnterpriseCollector) -> None:
        self._collector = collector

    async def render_metrics(self) -> str:
        scrape_data = await self._collector.collect()
        registry = CollectorRegistry(auto_describe=False)
        registry.register(StaticPrometheusCollector(scrape_data))
        return generate_latest(registry).decode("utf-8")


def _optional_int(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return float(value)
    return None


def _require_int(value: object, field_name: str, feature_name: str) -> float:
    number = _optional_int(value)
    if number is None:
        raise GitHubApiError(
            f"Feature '{feature_name}' payload missing required integer field '{field_name}'."
        )
    return number


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Environment variable {name} must be boolean-like, got {raw!r}")


async def _handle_healthz(_: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


async def _handle_metrics(request: web.Request) -> web.Response:
    service: ExportService = request.app["service"]
    try:
        payload = await service.render_metrics()
    except GitHubApiError:
        LOGGER.exception("Scrape failed")
        return web.Response(
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            text="scrape failed\n",
            content_type="text/plain",
        )
    return web.Response(text=payload, headers={"Content-Type": CONTENT_TYPE_LATEST})


async def _run_http_server(
    service: ExportService, listen_address: str, port: int, enterprise: str
) -> None:
    app = web.Application()
    app["service"] = service
    app.router.add_get("/", _handle_healthz)
    app.router.add_get("/healthz", _handle_healthz)
    app.router.add_get("/metrics", _handle_metrics)
    runner = web.AppRunner(app, access_log=LOGGER)
    await runner.setup()
    site = web.TCPSite(runner, host=listen_address, port=port)
    await site.start()
    LOGGER.info(
        "Exporter started for enterprise=%s at http://%s:%d/metrics",
        enterprise,
        listen_address,
        port,
    )
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()


async def _run(args: argparse.Namespace) -> None:
    client = GitHubClient(
        api_url=args.api_url,
        token=args.token,
        timeout_seconds=args.timeout_seconds,
    )
    collector = EnterpriseCollector(client=client, enterprise=args.enterprise)
    service = ExportService(collector=collector)
    if args.once:
        print(await service.render_metrics(), end="")
        return
    await _run_http_server(
        service=service,
        listen_address=args.listen_address,
        port=args.port,
        enterprise=args.enterprise,
    )


def run_export(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Enterprise-level GitHub metrics exporter.")
    parser.add_argument(
        "--enterprise",
        default=os.getenv("GITHUB_ENTERPRISE"),
        help="GitHub enterprise slug (env: GITHUB_ENTERPRISE).",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_TOKEN"),
        help="GitHub API token (env: GITHUB_TOKEN).",
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("GITHUB_API_URL", "https://api.github.com"),
        help="GitHub API URL (env: GITHUB_API_URL).",
    )
    parser.add_argument(
        "--listen-address",
        default=os.getenv("EXPORTER_LISTEN_ADDRESS", "0.0.0.0"),
        help="Address to bind exporter HTTP server (env: EXPORTER_LISTEN_ADDRESS).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("EXPORTER_PORT", "9736")),
        help="Exporter HTTP port (env: EXPORTER_PORT).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.getenv("EXPORTER_TIMEOUT_SECONDS", "15")),
        help="GitHub API timeout in seconds (env: EXPORTER_TIMEOUT_SECONDS).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=_parse_bool_env("EXPORTER_ONCE", False),
        help="Print metrics once and exit (env: EXPORTER_ONCE).",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Log level (env: LOG_LEVEL).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.enterprise:
        raise ValueError("Missing enterprise slug. Use --enterprise or GITHUB_ENTERPRISE.")
    if not args.token:
        raise ValueError("Missing token. Use --token or GITHUB_TOKEN.")
    if args.port <= 0:
        raise ValueError("Port must be a positive integer.")
    if args.timeout_seconds <= 0:
        raise ValueError("Timeout must be greater than zero.")

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        LOGGER.info("Exporter shutdown requested.")
