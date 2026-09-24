from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.config import ExperimentConfig
from app.parameters.models import ParameterSpec
from app.testing.measurements import MeasurementSuite
from app.testing.runner import ExperimentRunner, prepare_inbound_payload


class FakePanel:
    async def get_inbound(self, _inbound_id: int) -> dict[str, Any]:
        return {"id": 9, "port": 9443, "protocol": "vless", "streamSettings": {}}


class FakeManager:
    mode = "clone"
    source_id = 1
    test_inbound_id = 9
    cleaned = False

    async def prepare(self) -> int:
        return self.test_inbound_id

    async def cleanup(self) -> None:
        self.cleaned = True


class BootstrapRunner(ExperimentRunner):
    def __init__(self, config: ExperimentConfig, manager: FakeManager):
        super().__init__(config, FakePanel())  # type: ignore[arg-type]
        self.manager = manager

    async def preflight(self) -> dict[str, Any]:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        return {"openapi_version": "3.8.0", "xray_version": "26.9.9", "manager": self.manager,
                "source": {"streamSettings": {}}}

    def _candidate_error(self, values: dict[str, Any], base_payload: dict[str, Any], server_address: str) -> str | None:
        return None

    async def _run_controls(self, manager: FakeManager, base_payload: dict[str, Any], server_address: str) -> None:
        return None

    async def _one_run(self, test_id: int, run_number: int, values: dict[str, Any], digest: str,
                       manager: FakeManager, base_payload: dict[str, Any], server_address: str,
                       *, include_ping: bool = False) -> dict[str, Any]:
        del manager, base_payload, server_address, include_ping
        status = "FAILED" if test_id == 1 else "OK"
        return {"test_id": test_id, "run": run_number, "timestamp": "now",
                "configuration_hash": digest, "configuration": values,
                "result": {"status": status, "score": 0.0 if status == "FAILED" else 1.0}}


def mutation_config(output: Path) -> ExperimentConfig:
    parameter = ParameterSpec.from_dict("transport", {
        "type": "enum", "values": ["broken", "working-a", "working-b"],
        "baseline": "broken", "mutate": True, "path": ["streamSettings", "network"],
    })
    return ExperimentConfig(
        panel={"url": "https://localhost"},
        inbound={"mode": "clone", "source_id": 1, "test_port": 9443},
        parameters=(parameter,),
        testing={"combination_strategy": "mutation", "mutation_generations": 2,
                 "max_combinations": 3, "runs_per_combination": 1,
                 "server_address": "example.test", "urls": ["https://example.com"]},
        output={"directory": str(output), "formats": ["json"]},
    )


def test_failed_baseline_still_bootstraps_first_mutation_generation(tmp_path: Path) -> None:
    manager = FakeManager()
    summary = asyncio.run(BootstrapRunner(mutation_config(tmp_path), manager).run())
    records = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert summary["configurations_tested"] == 3
    assert summary["completed"] == 3
    assert summary["termination_reason"] == "limit_reached"
    assert [item["result"]["status"] for item in records] == ["FAILED", "OK", "OK"]
    assert manager.cleaned is True


def test_failed_configuration_stops_after_configured_failure_threshold(tmp_path: Path) -> None:
    config = mutation_config(tmp_path)
    config.testing["runs_per_combination"] = 3
    config.testing["max_failed_runs"] = 1
    summary = asyncio.run(BootstrapRunner(config, FakeManager()).run())
    records = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()]

    # The broken baseline is recorded once, while each working candidate receives all repeats.
    assert summary["completed"] == 7
    assert [item["test_id"] for item in records].count(1) == 1
    assert [item["test_id"] for item in records].count(2) == 3
    assert [item["test_id"] for item in records].count(3) == 3


def test_quality_and_screening_gates_are_strict_by_default(tmp_path: Path) -> None:
    runner = ExperimentRunner(mutation_config(tmp_path), FakePanel())  # type: ignore[arg-type]
    screen_ok, failures = runner._screening_gate({"tcp_connect_ok": True, "success_rate": 0.5})
    assert screen_ok is False and "success_rate<1" in failures
    ok, failures, score = runner._quality_gate({
        "tcp_connect_ok": True, "success_rate": 1.0, "successful_requests": 3,
        "latency_p95_ms": 100.0,
    })
    assert ok is True and failures == [] and 0 < score <= 1


def test_kcp_skips_tcp_connect_gate_and_probe(tmp_path: Path) -> None:
    suite = MeasurementSuite({"urls": ["https://example.com"]}, timeout=1,
                             target_host="example.com", target_port=443, transport="kcp")
    connectivity = asyncio.run(suite._connectivity_metrics())
    assert connectivity == {"tcp_connect_supported": False}

    runner = ExperimentRunner(mutation_config(tmp_path), FakePanel())  # type: ignore[arg-type]
    assert runner._screening_gate({"tcp_connect_supported": False, "success_rate": 1.0}) == (True, [])
    quality_ok, failures, _ = runner._quality_gate({
        "tcp_connect_supported": False, "success_rate": 1.0, "successful_requests": 1,
    })
    assert quality_ok and failures == []


def test_transport_switch_drops_incompatible_vless_flow_from_json_settings() -> None:
    payload = {
        "protocol": "vless",
        "streamSettings": '{"network":"tcp","security":"tls"}',
        "settings": '{"clients":[{"id":"redacted","flow":"xtls-rprx-vision"}]}',
    }
    result = prepare_inbound_payload(payload, {"network": "ws", "security": "tls"})
    assert isinstance(result["streamSettings"], str)
    assert isinstance(result["settings"], str)
    assert "flow" not in json.loads(result["settings"])["clients"][0]


def test_tcp_tls_keeps_vless_flow_on_inbound() -> None:
    payload = {
        "protocol": "vless",
        "streamSettings": {"network": "tcp", "security": "tls"},
        "settings": {"clients": [{"id": "redacted", "flow": "xtls-rprx-vision"}]},
    }
    result = prepare_inbound_payload(payload, {"network": "tcp", "security": "tls"})
    assert result["settings"]["clients"][0]["flow"] == "xtls-rprx-vision"


def test_kcp_timeout_explains_udp_requirement() -> None:
    diagnostic = ExperimentRunner._transport_diagnostic(
        "kcp", {"last_http_error": "ConnectTimeout: "},
    )
    assert diagnostic and "UDP" in diagnostic and "kcpSettings" in diagnostic


def test_invalid_success_rate_is_rejected(tmp_path: Path) -> None:
    config = mutation_config(tmp_path)
    config.testing["quality_gates"] = {"min_success_rate": 1.2}
    runner = ExperimentRunner(config, FakePanel())  # type: ignore[arg-type]
    try:
        runner._quality_gate({"tcp_connect_ok": True, "success_rate": 1.0, "successful_requests": 1})
    except ValueError as error:
        assert "between 0 and 1" in str(error)
    else:
        raise AssertionError("invalid min_success_rate was accepted")
