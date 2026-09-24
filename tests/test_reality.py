from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import ExperimentConfig
from app.parameters.models import ParameterSpec
from app.testing.runner import ExperimentRunner
from app.xray.config_builder import ClientConfigBuilder
from app.xray.reality import (RealityCredentials, prepare_reality_inbound,
                              resolve_profile, validate_pair)


def source_inbound() -> dict:
    return {"id": 1, "port": 443, "protocol": "vless",
            "settings": {"clients": [{"id": "client-id", "flow": "xtls-rprx-vision"}]},
            "streamSettings": {"network": "tcp", "security": "tls", "tlsSettings": {}}}


def test_generated_credentials_roundtrip_and_resume_requires_file(tmp_path: Path) -> None:
    fake = type("Completed", (), {"stdout": "PrivateKey: " + "A" * 43 + "\nPassword (PublicKey): " + "B" * 43 + "\n"})()
    with patch("app.xray.reality.subprocess.run", return_value=fake) as process:
        credentials = RealityCredentials.generate("xray")
    assert process.call_args.args[0] == ["xray", "x25519"]
    assert len(credentials.short_id) == 16
    path = tmp_path / ".reality-credentials.json"
    credentials.save(path, "experiment")
    assert credentials.private_key in path.read_text(encoding="utf-8")
    assert RealityCredentials.load(path, "experiment") == credentials
    with pytest.raises(ValueError, match="checkpoint"):
        RealityCredentials.load(path, "other")
    path.unlink()
    with pytest.raises(FileNotFoundError, match="--resume"):
        RealityCredentials.load(path, "experiment")


def test_reality_profile_requires_explicit_target_for_tls_source() -> None:
    assert resolve_profile({}, source_inbound())[1] == "reality_target_or_sni_missing"
    assert resolve_profile({"reality": {"target": "example.com:443", "server_names": ["example.com"]}},
                           source_inbound())[1] is None
    assert resolve_profile({"reality": {"target": "https://example.com", "server_names": ["example.com"]}},
                           source_inbound())[1] == "reality_target_invalid"


def test_server_client_validation_catches_sni_short_id_and_xhttp_mismatches() -> None:
    profile, _ = resolve_profile({"reality": {"target": "example.com:443", "server_names": ["example.com"]}},
                                 source_inbound())
    assert profile is not None
    credentials = RealityCredentials("A" * 43, "B" * 43, "0123456789abcdef")
    inbound = source_inbound()
    inbound["streamSettings"] = {"network": "xhttp", "security": "reality",
                                 "xhttpSettings": {"path": "/", "mode": "auto"}}
    inbound = prepare_reality_inbound(inbound, profile, credentials)
    client = ClientConfigBuilder("proxy.example.com").build({}, [], inbound)
    assert validate_pair(inbound, client) is None
    changed = copy.deepcopy(client)
    changed["outbounds"][0]["streamSettings"]["realitySettings"]["serverName"] = "wrong.example.com"
    assert validate_pair(inbound, changed) == "reality_sni_mismatch"
    changed = copy.deepcopy(client)
    changed["outbounds"][0]["streamSettings"]["realitySettings"]["shortId"] = "aabb"
    assert validate_pair(inbound, changed) == "reality_short_id_mismatch"
    changed = copy.deepcopy(client)
    changed["outbounds"][0]["streamSettings"]["xhttpSettings"]["path"] = "/other"
    assert validate_pair(inbound, changed) == "xhttp_path_mismatch"


def test_panel_readback_detects_effective_security_change() -> None:
    before = source_inbound()
    after = copy.deepcopy(before)
    after["streamSettings"]["security"] = "none"
    assert ExperimentRunner._readback_error(before, after) == "panel_readback_transport_mismatch"
    assert ExperimentRunner._readback_error(before, copy.deepcopy(before)) is None


class SkipPanel:
    async def get_inbound(self, _inbound_id: int) -> dict:
        return source_inbound()


class SkipManager:
    mode = "clone"
    source_id = 1
    test_inbound_id = 2

    def __init__(self) -> None:
        self.applied = 0
        self.cleaned = False

    async def prepare(self) -> int:
        return self.test_inbound_id

    async def apply(self, payload: dict) -> None:
        self.applied += 1

    async def cleanup(self) -> None:
        self.cleaned = True


class SkipRunner(ExperimentRunner):
    def __init__(self, config: ExperimentConfig, manager: SkipManager):
        super().__init__(config, SkipPanel())  # type: ignore[arg-type]
        self.manager = manager

    async def preflight(self) -> dict:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        return {"openapi_version": "3.8", "xray_version": "26.9", "source": source_inbound(), "manager": self.manager}

    async def _run_controls(self, manager: SkipManager, base_payload: dict, server_address: str) -> None:
        return None


def skip_config(directory: Path) -> ExperimentConfig:
    security = ParameterSpec.from_dict("security", {"type": "enum", "values": ["reality"],
                                                     "path": ["streamSettings", "security"]})
    return ExperimentConfig({"url": "https://panel.example.com"},
                            {"mode": "clone", "source_id": 1, "test_port": 9443}, (security,),
                            testing={"server_address": "proxy.example.com", "urls": ["https://example.com"],
                                     "runs_per_combination": 5, "max_failed_runs": 3},
                            output={"directory": str(directory), "formats": ["json"]})


def test_invalid_reality_candidate_skips_once_without_panel_update_and_resume(tmp_path: Path) -> None:
    import asyncio
    manager = SkipManager()
    config = skip_config(tmp_path)
    first = asyncio.run(SkipRunner(config, manager).run())
    assert first["skipped"] == 1 and first["failed"] == 0 and manager.applied == 0
    records = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1 and records[0]["result"]["status"] == "SKIPPED"
    second = asyncio.run(SkipRunner(config, manager).run(resume=True))
    assert second["skipped"] == 1 and manager.applied == 0
    assert len((tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_controls_continue_after_failure_and_keep_raw_secrets_out(tmp_path: Path) -> None:
    import asyncio

    class ControlRunner(SkipRunner):
        async def _one_run(self, test_id, run_number, values, digest, manager, base_payload,
                           server_address, *, include_ping=False, screening_only=False):
            self.calls.append(base_payload["streamSettings"]["security"])
            return {"timestamp": "now", "result": {"status": "FAILED" if len(self.calls) == 1 else "OK",
                    "stage": "screening", "screening": {"success_rate": 0.0 if len(self.calls) == 1 else 1.0},
                    "client_xray_diagnostics": ["reality_handshake"]}}

    runner = ControlRunner(skip_config(tmp_path), SkipManager())
    runner.calls = []
    runner._reality_profile, _ = resolve_profile(
        {"reality": {"target": "example.com:443", "server_names": ["example.com"]}}, source_inbound())
    runner._reality_credentials = RealityCredentials("A" * 43, "B" * 43, "0123456789abcdef")
    asyncio.run(ExperimentRunner._run_controls(runner, runner.manager, source_inbound(), "proxy.example.com"))
    rows = [json.loads(line) for line in (tmp_path / "diagnostics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["profile"] for row in rows] == ["tcp_tls", "tcp_reality", "xhttp_reality"]
    assert [row["status"] for row in rows] == ["FAILED", "OK", "OK"]
    assert runner._reality_credentials.private_key not in (tmp_path / "diagnostics.jsonl").read_text(encoding="utf-8")
