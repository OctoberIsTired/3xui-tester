from __future__ import annotations

import asyncio
import re
import statistics
import subprocess
import sys
import time
from typing import Any

import httpx


class MeasurementSuite:
    def __init__(self, testing: dict[str, Any], timeout: float, socks_port: int = 10808,
                 target_host: str | None = None, target_port: int | None = None,
                 tcp_connect_timeout: float | None = None, transport: str = "tcp"):
        self.urls = list(testing.get("urls", []))
        self.latency_requests = int(testing.get("latency", {}).get("requests", 3))
        self.stability_requests = int(testing.get("stability", {}).get("requests", 5))
        self.ping = dict(testing.get("ping", {}))
        self.speed_test = dict(testing.get("speed_test", {}))
        self.target_host, self.target_port = target_host, target_port
        self.transport = transport
        if timeout <= 0:
            raise ValueError("request timeout must be greater than zero")
        self.timeout = timeout
        self.tcp_connect_timeout = tcp_connect_timeout if tcp_connect_timeout is not None else timeout
        if self.tcp_connect_timeout <= 0:
            raise ValueError("TCP connect timeout must be greater than zero")
        self.proxy = f"socks5://127.0.0.1:{socks_port}"

    async def screen(self, requests: int = 1) -> dict[str, Any]:
        """Cheap viability probe used before the complete measurement suite."""
        if not self.urls:
            raise ValueError("testing.urls must contain at least one URL")
        metrics: dict[str, Any] = {}
        if self.target_host and self.target_port:
            metrics.update(await self._connectivity_metrics())
        async with httpx.AsyncClient(proxy=self.proxy, timeout=self.timeout) as client:
            metrics.update(await self._http_metrics(client, requests, requests))
        return metrics

    async def run(self, *, include_ping: bool = True, include_speed: bool = True) -> dict[str, Any]:
        if not self.urls:
            raise ValueError("testing.urls must contain at least one URL")
        metrics: dict[str, Any] = {}
        if self.target_host and self.target_port:
            metrics.update(await self._connectivity_metrics())
        if include_ping and self.target_host and self.ping.get("enabled", True):
            metrics.update(await self._icmp_ping())
        async with httpx.AsyncClient(proxy=self.proxy, timeout=self.timeout) as client:
            metrics.update(await self._http_metrics(client, self.latency_requests, self.stability_requests))
            if include_speed and self.speed_test.get("enabled") and self.speed_test.get("url"):
                metrics.update(await self._download_speed(client))
        return metrics

    async def _http_metrics(self, client: httpx.AsyncClient, latency_requests: int,
                            stability_requests: int) -> dict[str, Any]:
        latencies: list[float] = []
        succeeded = failed = 0
        last_error: str | None = None
        for number in range(max(latency_requests, stability_requests)):
            started = time.perf_counter()
            try:
                response = await client.get(self.urls[number % len(self.urls)])
                response.raise_for_status()
                elapsed = (time.perf_counter() - started) * 1000
                if number < latency_requests:
                    latencies.append(elapsed)
                if number < stability_requests:
                    succeeded += 1
            except (httpx.HTTPError, TimeoutError) as error:
                last_error = f"{type(error).__name__}: {error}"
                if number < stability_requests:
                    failed += 1
        if not latencies:
            result: dict[str, Any] = {"successful_requests": succeeded, "failed_requests": failed, "success_rate": 0.0}
            if last_error:
                result["last_http_error"] = last_error
            return result
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, round((len(ordered) - 1) * .95))]
        result = {"connect_time_ms": latencies[0], "latency_min_ms": min(latencies),
                  "latency_avg_ms": statistics.mean(latencies), "latency_median_ms": statistics.median(latencies),
                  "latency_p95_ms": p95, "latency_max_ms": max(latencies),
                  "successful_requests": succeeded, "failed_requests": failed,
                  "success_rate": succeeded / (succeeded + failed) if succeeded + failed else 0.0}
        if last_error:
            result["last_http_error"] = last_error
        return result

    async def _tcp_connect(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.target_host, self.target_port), timeout=self.tcp_connect_timeout,
            )
            elapsed = (time.perf_counter() - started) * 1000
            writer.close()
            await writer.wait_closed()
            return {"tcp_connect_ms": elapsed, "tcp_connect_ok": True}
        except (OSError, TimeoutError):
            return {"tcp_connect_ok": False}

    async def _connectivity_metrics(self) -> dict[str, Any]:
        # mKCP listens on UDP, so a TCP handshake cannot say anything about
        # whether that transport is reachable. The proxy request in screen/run
        # is the actual end-to-end check for KCP.
        if self.transport == "kcp":
            return {"tcp_connect_supported": False}
        return {"tcp_connect_supported": True, **await self._tcp_connect()}

    async def _icmp_ping(self) -> dict[str, Any]:
        requests = max(1, int(self.ping.get("requests", 4)))
        timeout_ms = max(100, int(float(self.ping.get("timeout", 2)) * 1000))
        times: list[float] = []
        for _ in range(requests):
            args = (["ping", "-n", "1", "-w", str(timeout_ms), self.target_host]
                    if sys.platform == "win32" else
                    ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), self.target_host])
            try:
                completed = await asyncio.wait_for(
                    asyncio.to_thread(subprocess.run, args, capture_output=True, timeout=timeout_ms / 1000 + 1),
                    timeout=timeout_ms / 1000 + 2,
                )
                output, returncode = completed.stdout + completed.stderr, completed.returncode
                text = output.decode("oem" if sys.platform == "win32" else "utf-8", errors="replace")
                match = re.search(r"(?:time|время)\s*[=<]\s*([0-9.,]+)\s*(?:ms|мс)", text, re.IGNORECASE)
                if returncode == 0:
                    times.append(float(match.group(1).replace(",", ".")) if match else 0.0)
            except (OSError, TimeoutError):
                pass
        received = len(times)
        result: dict[str, Any] = {"ping_sent": requests, "ping_received": received,
                                  "ping_packet_loss_pct": (requests - received) * 100 / requests}
        if times:
            result.update({"ping_min_ms": min(times), "ping_avg_ms": statistics.mean(times),
                           "ping_max_ms": max(times), "ping_jitter_ms": statistics.pstdev(times) if len(times) > 1 else 0.0})
        return result

    async def _download_speed(self, client: httpx.AsyncClient) -> dict[str, Any]:
        maximum = max(1, int(self.speed_test.get("max_bytes", 5_000_000)))
        downloaded = 0
        started = time.perf_counter()
        try:
            async with client.stream("GET", str(self.speed_test["url"])) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    downloaded += len(chunk)
                    if downloaded >= maximum:
                        break
            elapsed = time.perf_counter() - started
            return {"download_bytes": downloaded, "download_seconds": elapsed,
                    "download_mbps": downloaded * 8 / elapsed / 1_000_000 if elapsed else 0.0}
        except (httpx.HTTPError, TimeoutError) as error:
            return {"download_bytes": downloaded, "download_error": f"{type(error).__name__}: {error}"}
