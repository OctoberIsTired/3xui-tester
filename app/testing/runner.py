from __future__ import annotations

import asyncio
import copy
import itertools
import json
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable
from urllib.parse import urlparse

from app.api.base import PanelClient
from app.api.three_xui import PanelAPIError
from app.config import ExperimentConfig, offline_warnings
from app.inbound.manager import InboundManager
from app.parameters.generator import Combination, CombinationGenerator, configuration_hash
from app.parameters.mapping import apply_mapping
from app.results.writer import ResultStore
from app.security.masking import mask_parameter_values, mask_secrets
from app.state.checkpoint import Checkpoint
from app.xray.client import XrayClient, XrayClientError, categorize_xray_logs
from app.xray.config_builder import ClientConfigBuilder
from app.xray.reality import (RealityCredentials, RealityProfile, prepare_reality_inbound,
                              resolve_profile, stream_settings, validate_pair)
from app.testing.measurements import MeasurementSuite


def prepare_inbound_payload(payload: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Remove protocol options inherited from the source that the new transport cannot use."""
    if payload.get("protocol") != "vless":
        return payload

    stream_raw = payload.get("streamSettings", {})
    stream_was_text = isinstance(stream_raw, str)
    try:
        stream = json.loads(stream_raw) if stream_was_text else stream_raw
    except json.JSONDecodeError as error:
        raise ValueError("inbound.streamSettings must be valid JSON") from error
    if not isinstance(stream, dict):
        return payload

    # xtls-rprx-vision is valid on raw TCP only. The source `test` inbound
    # uses Vision, so carrying its client setting into WS/gRPC/KCP/etc makes
    # Xray reject each outbound before it even reaches the server.
    network = str(values.get("network", stream.get("network", "tcp")))
    security = str(values.get("security", stream.get("security", "none")))
    if network != "tcp" or security not in {"tls", "reality"}:
        settings_raw = payload.get("settings", {})
        settings_was_text = isinstance(settings_raw, str)
        try:
            settings = json.loads(settings_raw) if settings_was_text else settings_raw
        except json.JSONDecodeError as error:
            raise ValueError("inbound.settings must be valid JSON") from error
        if isinstance(settings, dict):
            for client in settings.get("clients", []):
                if isinstance(client, dict):
                    client.pop("flow", None)
            payload["settings"] = (json.dumps(settings, ensure_ascii=False, separators=(",", ":"))
                                   if settings_was_text else settings)

    payload["streamSettings"] = (json.dumps(stream, ensure_ascii=False, separators=(",", ":"))
                                 if stream_was_text else stream)
    return payload


@dataclass(frozen=True)
class SearchCandidate:
    combination: Combination
    changed: frozenset[str]
    new_changes: frozenset[str]
    parent_score: float


class CandidateValidationError(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class ExperimentRunner:
    def __init__(self, config: ExperimentConfig, panel: PanelClient,
                 progress_callback: Callable[[dict[str, Any]], None] | None = None):
        self.config, self.panel = config, panel
        self.generator = CombinationGenerator(config.parameters)
        self.stop_requested = False
        self.progress_callback = progress_callback
        self._last_inbound_payload_hash: str | None = None
        self._active_inbound: dict[str, Any] | None = None
        self._reality_profile: RealityProfile | None = None
        self._reality_profile_error: str | None = None
        self._reality_credentials: RealityCredentials | None = None

    def request_stop(self) -> None:
        self.stop_requested = True

    async def preflight(self) -> dict[str, Any]:
        try:
            __import__("socksio")
        except ImportError as error:
            raise RuntimeError("SOCKS measurements require project dependencies: run through 'uv run' or the project .venv") from error
        if "xlsx" in self.config.output.get("formats", []):
            try:
                __import__("openpyxl")
            except ImportError as error:
                raise RuntimeError("XLSX export requires openpyxl: run through 'uv run' or the project .venv") from error
        await self.panel.authenticate()
        openapi = await self.panel.discover()
        manager = InboundManager(self.panel, self.config.inbound.get("mode", "clone"), int(self.config.inbound["source_id"]),
                                 tuple(self.config.testing.get("port_range", [20000, 30000])),
                                 test_port=int(self.config.inbound["test_port"]) if self.config.inbound.get("test_port") is not None else None,
                                 backup_path=self.config.output_dir / "source-inbound-backup.json")
        source = await manager.preflight()
        status = await self.panel.server_status()
        if status.get("xray", {}).get("state") != "running":
            raise RuntimeError("Panel reports that Xray is not running")
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        if not self.config.testing.get("urls"):
            raise ValueError("testing.urls is required for a non-dry run")
        return {"openapi_version": openapi.get("info", {}).get("version"), "xray_version": status.get("xray", {}).get("version"),
                "source": source, "manager": manager}

    async def run(self, *, dry_run: bool = False, resume: bool = False, max_tests: int | None = None) -> dict[str, Any]:
        prepared = await self.preflight()
        manager: InboundManager = prepared["manager"]
        self._reality_profile, self._reality_profile_error = resolve_profile(self.config.testing, prepared["source"])
        server_address = str(self.config.testing.get("server_address") or urlparse(self.config.panel["url"]).hostname or "")
        warnings = self._warnings()
        configured_limit = self.config.max_combinations
        effective_limit = min(max_tests, configured_limit) if max_tests is not None else configured_limit
        if self.config.combination_strategy == "mutation":
            planned = self.generator.mutations()
            combinations = iter(())
            preview = planned[:min(10, effective_limit)]
            planned_count = effective_limit
        elif self.config.combination_strategy == "pairwise":
            planned = self.generator.pairwise()
            combinations = iter(planned[:effective_limit])
            preview = planned[:min(10, effective_limit)]
            planned_count = min(len(planned), effective_limit)
        else:
            combinations = itertools.islice(self.generator.iter_valid(), effective_limit)
            preview = list(itertools.islice(self.generator.iter_valid(), min(10, effective_limit)))
            planned_count = min(self.generator.raw_count, effective_limit)
        if dry_run:
            candidates = (planned[:effective_limit] if self.config.combination_strategy != "exhaustive" else
                          list(itertools.islice(self.generator.iter_valid(), effective_limit)))
            # Only mutation seeds are knowable before the adaptive search runs.
            placeholders = RealityCredentials("A" * 43, "B" * 43, "0123456789abcdef")
            self._reality_credentials = placeholders
            skipped = sum(self._candidate_error(item.values, prepared["source"], server_address) is not None
                          for item in candidates)
            self._reality_credentials = None
            response = {"strategy": self.config.combination_strategy, "raw_combinations": self.generator.raw_count,
                    "planned_combinations": len(candidates) if self.config.combination_strategy == "exhaustive" else planned_count,
                    "limit": effective_limit,
                    "preview": [self._public_values(item.values) for item in preview],
                    "metadata": {"openapi_version": prepared["openapi_version"], "xray_version": prepared["xray_version"],
                                 "source_inbound_id": manager.source_id}, "warnings": warnings}
            response["skipped_initial_candidates" if self.config.combination_strategy == "mutation" else
                     "skipped_combinations"] = skipped
            return response
        if manager.mode == "existing" and not self.config.inbound.get("allow_existing", False):
            raise PermissionError("existing mode requires inbound.allow_existing: true")
        store = ResultStore(self.config.output_dir)
        checkpoint_path = self.config.output_dir / "state.json"
        checkpoint = Checkpoint.load(checkpoint_path) if resume else None
        if resume and checkpoint is None:
            raise FileNotFoundError(f"--resume requested but {checkpoint_path} does not exist")
        experiment_id = checkpoint.experiment_id if checkpoint else datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        search_signature = self._search_signature()
        checkpoint = checkpoint or Checkpoint(experiment_id, {"started_at": datetime.now(UTC).isoformat(),
            "three_xui_version": prepared["openapi_version"], "xray_version": prepared["xray_version"],
            "source_inbound_id": manager.source_id, "search_signature": search_signature})
        if resume and checkpoint.metadata.get("search_signature") not in {None, search_signature}:
            raise ValueError("The configuration changed since this checkpoint; refusing an inconsistent resume")
        credentials_path = self.config.output_dir / ".reality-credentials.json"
        if self._reality_profile:
            if resume:
                if checkpoint.metadata.get("reality_credentials"):
                    self._reality_credentials = RealityCredentials.load(credentials_path, experiment_id)
                elif any(item.get("result", {}).get("status") != "SKIPPED" for item in store.records()):
                    raise ValueError("Cannot resume an older REALITY experiment without saved credentials")
                else:
                    self._reality_credentials = RealityCredentials.generate(str(self.config.testing.get("xray_binary", "xray")))
                    self._reality_credentials.save(credentials_path, experiment_id)
            else:
                self._reality_credentials = RealityCredentials.generate(str(self.config.testing.get("xray_binary", "xray")))
                self._reality_credentials.save(credentials_path, experiment_id)
            checkpoint.metadata["reality_credentials"] = True
        previous_records = store.records() if resume else []
        records_by_key = {f"{item['configuration_hash']}:{item['run']}": item for item in previous_records}
        if resume:
            checkpoint.completed_keys = set(records_by_key)
            checkpoint.failed = sum(item.get("result", {}).get("status") not in {"OK", "SKIPPED"} for item in previous_records)
        completed = len(records_by_key)
        skipped = sum(item.get("result", {}).get("status") == "SKIPPED" for item in previous_records)
        configurations_tested = 0
        interrupted = False
        termination_reason: str | None = None
        cleanup_error: str | None = None
        export_error: str | None = None
        try:
            test_id = await manager.prepare()
            base_payload = await self.panel.get_inbound(test_id)
            self._last_inbound_payload_hash = configuration_hash(base_payload)
            self._active_inbound = base_payload
            await self._run_controls(manager, base_payload, server_address)
            # Controls deliberately change the test inbound; the first search
            # candidate must always be applied again, even if hashes coincide.
            self._last_inbound_payload_hash = None
            self._active_inbound = None

            async def execute(number: int, combination: Combination,
                              generation: int | None = None) -> tuple[bool, float]:
                nonlocal completed, skipped
                all_ok = True
                scores: list[float] = []
                failed_runs = 0
                first_key = f"{combination.hash}:1"
                if records_by_key.get(first_key, {}).get("result", {}).get("status") == "SKIPPED":
                    return False, 0.0
                reason = self._candidate_error(combination.values, base_payload, server_address)
                if reason:
                    if first_key not in records_by_key:
                        record = self._skipped_record(number, combination, reason)
                        store.append(record)
                        records_by_key[first_key] = record
                        checkpoint.completed_keys.add(first_key)
                        checkpoint.save(checkpoint_path)
                        completed += 1
                        skipped += 1
                        if self.progress_callback:
                            self.progress_callback({"completed": completed, "failed": checkpoint.failed,
                                                    "skipped": skipped, "test_id": number, "run": 1,
                                                    "configuration": self._public_values(combination.values), "result": record["result"]})
                    return False, 0.0
                for run_number in range(1, self.config.runs_per_combination + 1):
                    key = f"{combination.hash}:{run_number}"
                    record = records_by_key.get(key)
                    if record is None:
                        if self.stop_requested:
                            return False, 0.0
                        record = await self._one_run(
                            number, run_number, combination.values, combination.hash,
                            manager, base_payload, server_address,
                            include_ping=number == 1 and run_number == 1,
                        )
                        store.append(record)
                        records_by_key[key] = record
                        if record["result"]["status"] not in {"OK", "SKIPPED"}:
                            checkpoint.failed += 1
                            store.error(record)
                        checkpoint.completed_keys.add(key)
                        checkpoint.save(checkpoint_path)
                        completed += 1
                        if self.progress_callback:
                            update = {"completed": completed, "failed": checkpoint.failed, "skipped": skipped,
                                      "test_id": number, "run": run_number,
                                      "configuration": self._public_values(combination.values), "result": record["result"]}
                            if generation is not None:
                                update["generation"] = generation
                            self.progress_callback(update)
                        await asyncio.sleep(float(self.config.testing.get("test_delay", 0)))
                    result = record.get("result", {})
                    if result.get("status") != "OK":
                        all_ok = False
                        failed_runs += 1
                    scores.append(float(result.get("score", 0.0)))
                    if failed_runs >= self.config.max_failed_runs:
                        break
                return all_ok, statistics.mean(scores) if scores else 0.0

            if self.config.combination_strategy == "mutation":
                max_depth = self.config.mutation_generations
                beam_width = self.config.beam_width
                children_per_parent = self.config.children_per_parent
                root, root_changes = self.generator.mutation_root()
                tested = {root.hash}
                coverage: Counter[str] = Counter()
                number = 1
                root_ok, root_score = await execute(number, root, 0)
                configurations_tested = 1
                parents: list[tuple[Combination, frozenset[str], float]] = [(root, root_changes, root_score)]
                for generation in range(1, max_depth + 1):
                    if self.stop_requested:
                        interrupted = True
                        termination_reason = "stopped"
                        break
                    # Generation 1 is a bootstrap around the baseline even when
                    # the baseline failed. Later generations expand only OK parents.
                    if generation > 1 and not parents:
                        termination_reason = "no_working_parents"
                        break
                    remaining = effective_limit - configurations_tested
                    if remaining <= 0:
                        termination_reason = "limit_reached"
                        break
                    pool = self._mutation_pool(
                        parents, tested, coverage,
                        None if generation == 1 else children_per_parent,
                    )
                    if not pool:
                        termination_reason = ("no_mutation_candidates" if generation == 1
                                              else "no_further_mutations")
                        break
                    selected = self._select_candidates(pool, remaining, coverage)
                    selected.sort(key=lambda item: self._payload_hash(base_payload, item.combination.values))
                    working_nodes: list[tuple[Combination, frozenset[str], float]] = []
                    for candidate in selected:
                        if self.stop_requested:
                            interrupted = True
                            termination_reason = "stopped"
                            break
                        number += 1
                        working, score = await execute(number, candidate.combination, generation)
                        configurations_tested += 1
                        tested.add(candidate.combination.hash)
                        coverage.update(candidate.new_changes)
                        if working:
                            working_nodes.append((candidate.combination, candidate.changed, score))
                    if interrupted:
                        break
                    parents = self._select_beam(working_nodes, beam_width)
                if termination_reason is None:
                    termination_reason = ("limit_reached" if configurations_tested >= effective_limit
                                          else "generation_limit_reached")
            else:
                for number, combination in enumerate(combinations, start=1):
                    if self.stop_requested:
                        interrupted = True
                        termination_reason = "stopped"
                        break
                    await execute(number, combination)
                    configurations_tested += 1
                if termination_reason is None:
                    termination_reason = ("limit_reached" if configurations_tested >= effective_limit
                                          else "plan_exhausted")
        finally:
            try:
                await manager.cleanup()
            except Exception as error:
                cleanup_error = f"{type(error).__name__}: {error}"
            checkpoint.save(checkpoint_path)
            try:
                store.export(self.config.output.get("formats", ["csv", "json"]))
            except Exception as error:
                export_error = f"{type(error).__name__}: {error}"
        result = {"experiment_id": experiment_id, "completed": completed,
                  "configurations_tested": configurations_tested, "failed": checkpoint.failed,
                  "skipped": skipped, "warnings": warnings, "termination_reason": termination_reason}
        if interrupted:
            result["interrupted"] = True
        if cleanup_error:
            result["cleanup_error"] = cleanup_error
        if export_error:
            result["export_error"] = export_error
        return result

    def _warnings(self) -> list[str]:
        warnings = offline_warnings(self.config)
        if self._reality_profile_error:
            warnings = [item for item in warnings if not item.startswith("REALITY is selected")]
            warnings.append(f"REALITY candidates will be skipped: {self._reality_profile_error}.")
        return warnings

    def _builder(self, server_address: str) -> ClientConfigBuilder:
        return ClientConfigBuilder(server_address, int(self.config.testing.get("socks_port", 10808)),
                                   tls_server_name=self.config.testing.get("tls_server_name"),
                                   verify_peer_cert_by_name=self.config.testing.get("verify_peer_cert_by_name"),
                                   pinned_peer_cert_sha256=self.config.testing.get("pinned_peer_cert_sha256"))

    def _candidate_payload(self, values: dict[str, Any], base_payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        try:
            payload = prepare_inbound_payload(
                apply_mapping(base_payload, values, self.config.parameters), values,
            )
            if stream_settings(payload).get("security", "none") == "reality":
                if self._reality_profile is None:
                    return None, self._reality_profile_error or "reality_target_or_sni_missing"
                if self._reality_credentials is None:
                    return None, "reality_credentials_missing"
                payload = prepare_reality_inbound(payload, self._reality_profile, self._reality_credentials)
            return payload, None
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None, "inbound_mapping_invalid"

    def _candidate_error(self, values: dict[str, Any], base_payload: dict[str, Any], server_address: str) -> str | None:
        payload, reason = self._candidate_payload(values, base_payload)
        if reason:
            return reason
        assert payload is not None
        try:
            client_config = self._builder(server_address).build(values, self.config.parameters, payload)
            reason = validate_pair(payload, client_config)
            if reason:
                return reason
            if stream_settings(payload).get("security") == "reality" and self._reality_credentials:
                reality = stream_settings(payload).get("realitySettings", {})
                if isinstance(reality, str):
                    reality = json.loads(reality)
                client = client_config["outbounds"][0]["streamSettings"].get("realitySettings", {})
                if (reality.get("privateKey") != self._reality_credentials.private_key or
                        client.get("password") != self._reality_credentials.password):
                    return "reality_key_mismatch"
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return "client_config_invalid"
        return None

    @staticmethod
    def _readback_error(expected: dict[str, Any], actual: dict[str, Any]) -> str | None:
        before, after = stream_settings(expected), stream_settings(actual)
        for key in ("network", "security"):
            if before.get(key) != after.get(key):
                return "panel_readback_transport_mismatch"
        if before.get("security") == "reality":
            left, right = before.get("realitySettings", {}), after.get("realitySettings", {})
            if isinstance(left, str):
                left = json.loads(left)
            if isinstance(right, str):
                right = json.loads(right)
            if (left.get("target") or left.get("dest")) != (right.get("target") or right.get("dest")):
                return "panel_readback_reality_mismatch"
            for key in ("serverNames", "privateKey", "shortIds"):
                if left.get(key) != right.get(key):
                    return "panel_readback_reality_mismatch"
        if before.get("network") == "xhttp":
            left, right = before.get("xhttpSettings", {}), after.get("xhttpSettings", {})
            if isinstance(left, str):
                left = json.loads(left)
            if isinstance(right, str):
                right = json.loads(right)
            for key, default in (("path", "/"), ("host", ""), ("mode", "auto")):
                if left.get(key, default) != right.get(key, default):
                    return "panel_readback_xhttp_mismatch"
        return None

    def _public_values(self, values: dict[str, Any]) -> dict[str, Any]:
        return mask_parameter_values(values, self.config.parameters)

    def _redact_result(self, value: Any) -> Any:
        secrets_to_hide = [str(self.config.panel.get(key, "")) for key in ("api_token", "password")]
        if self._reality_credentials:
            secrets_to_hide.extend((self._reality_credentials.private_key,
                                    self._reality_credentials.password,
                                    self._reality_credentials.short_id))
        sensitive = [item for item in secrets_to_hide if item and not item.startswith("${")]
        if isinstance(value, dict):
            return mask_secrets({key: self._redact_result(item) for key, item in value.items()})
        if isinstance(value, list):
            return [self._redact_result(item) for item in value]
        if isinstance(value, str):
            for item in sensitive:
                value = value.replace(item, "***")
        return value

    def _skipped_record(self, number: int, combination: Combination, reason: str) -> dict[str, Any]:
        return {"test_id": number, "run": 1, "timestamp": datetime.now(UTC).isoformat(),
                "configuration_hash": combination.hash, "configuration": self._public_values(combination.values),
                "result": {"status": "SKIPPED", "stage": "validation", "reason_code": reason}}

    def _control_payload(self, base_payload: dict[str, Any], network: str, security: str) -> dict[str, Any]:
        payload = copy.deepcopy(base_payload)
        stream = stream_settings(payload)
        stream["network"], stream["security"] = network, security
        if network == "xhttp":
            stream["xhttpSettings"] = {"path": "/", "mode": "auto", "host": ""}
        payload["streamSettings"] = (json.dumps(stream, ensure_ascii=False, separators=(",", ":"))
                                     if isinstance(payload.get("streamSettings"), str) else stream)
        return payload

    async def _run_controls(self, manager: InboundManager, base_payload: dict[str, Any], server_address: str) -> None:
        source_stream = stream_settings(base_payload)
        profiles: list[tuple[str, dict[str, Any] | None]] = [
            ("tcp_tls", base_payload if source_stream.get("network", "tcp") == "tcp" and
             source_stream.get("security") == "tls" else None),
            ("tcp_reality", self._control_payload(base_payload, "tcp", "reality")),
            ("xhttp_reality", self._control_payload(base_payload, "xhttp", "reality")),
        ]
        path = self.config.output_dir / "diagnostics.jsonl"
        for name, payload in profiles:
            if self.stop_requested:
                break
            reason = "control_profile_unavailable" if payload is None else self._candidate_error({}, payload, server_address)
            if reason:
                diagnostic = {"timestamp": datetime.now(UTC).isoformat(), "profile": name,
                              "status": "SKIPPED", "reason_code": reason}
            else:
                assert payload is not None
                record = await self._one_run(0, 1, {}, configuration_hash({"control": name}), manager,
                                             payload, server_address, screening_only=True)
                result = record["result"]
                screen = result.get("screening", {})
                diagnostic = {"timestamp": record["timestamp"], "profile": name,
                              "status": result.get("status"), "stage": result.get("stage"),
                              "reason_code": result.get("reason_code"),
                              "tcp_connect_ok": screen.get("tcp_connect_ok"),
                              "success_rate": screen.get("success_rate"),
                              "client_xray_diagnostics": result.get("client_xray_diagnostics", []),
                              "server_xray_diagnostics": result.get("server_xray_diagnostics", []),
                              "server_xray_log_status": result.get("server_xray_log_status")}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(mask_secrets(diagnostic), ensure_ascii=False, sort_keys=True) + "\n")

    async def _one_run(self, test_id: int, run_number: int, values: dict[str, Any], digest: str, manager: InboundManager,
                       base_payload: dict[str, Any], server_address: str, *, include_ping: bool = False,
                       screening_only: bool = False) -> dict[str, Any]:
        started = datetime.now(UTC).isoformat()
        result: dict[str, Any]
        client: XrayClient | None = None
        try:
            payload, reason = self._candidate_payload(values, base_payload)
            if reason or payload is None:
                raise CandidateValidationError(reason or "inbound_mapping_invalid")
            payload_hash = configuration_hash(payload)
            if payload_hash != self._last_inbound_payload_hash or self._active_inbound is None:
                await manager.apply(payload)
                await asyncio.sleep(float(self.config.testing.get("config_apply_delay", 0)))
                self._active_inbound = await self.panel.get_inbound(manager.test_inbound_id or manager.source_id)
                self._last_inbound_payload_hash = payload_hash
            current = self._active_inbound
            readback_error = self._readback_error(payload, current)
            if readback_error:
                raise CandidateValidationError(readback_error)
            transport = str(stream_settings(current).get("network", "tcp"))
            builder = self._builder(server_address)
            client_config = builder.build(values, self.config.parameters, current)
            pair_error = validate_pair(current, client_config)
            if pair_error:
                raise CandidateValidationError(pair_error)
            client = XrayClient(str(self.config.testing.get("xray_binary", "xray")), float(self.config.timeouts.get("xray_start", 15)),
                                config_directory=self.config.output_dir / ".xray-runtime")
            try:
                await client.start(client_config)
                request_timeout = float(self.config.timeouts.get("request", 5))
                tcp_connect_timeout = float(self.config.timeouts.get("tcp_connect", min(request_timeout, 3)))
                suite = MeasurementSuite(self.config.testing, request_timeout, builder.socks_port,
                                         server_address, int(current["port"]), tcp_connect_timeout,
                                         transport=transport)
                screening = dict(self.config.testing.get("screening", {}))
                screening_metrics: dict[str, Any] | None = None
                if screening_only or screening.get("enabled", True):
                    screening_timeout = float(self.config.timeouts.get("screening", 8))
                    try:
                        screening_metrics = await asyncio.wait_for(
                            suite.screen(1 if screening_only else max(1, int(screening.get("requests", 1)))),
                            timeout=screening_timeout,
                        )
                    except TimeoutError:
                        screening_metrics = {"successful_requests": 0, "failed_requests": 1,
                                             "success_rate": 0.0, "screening_timed_out": True}
                    screen_ok, screen_failures = self._screening_gate(screening_metrics)
                    if not screen_ok:
                        result = {"status": "FAILED", "stage": "screening", "score": 0.0,
                                  "gate_failures": screen_failures, "screening": screening_metrics}
                        diagnostic = self._transport_diagnostic(transport, screening_metrics)
                        if diagnostic:
                            result["transport_diagnostic"] = diagnostic
                    elif screening_only:
                        result = {"status": "OK", "stage": "screening", "score": 1.0,
                                  "screening": screening_metrics}
                    else:
                        metrics = await suite.run(include_ping=include_ping)
                        ok, failures, score = self._quality_gate(metrics)
                        result = {"status": "OK" if ok else "FAILED", "stage": "measurement",
                                  "score": score, "gate_failures": failures,
                                  "screening": screening_metrics, **metrics}
                else:
                    metrics = await suite.run(include_ping=include_ping)
                    ok, failures, score = self._quality_gate(metrics)
                    result = {"status": "OK" if ok else "FAILED", "stage": "measurement",
                              "score": score, "gate_failures": failures, **metrics}
            finally:
                await client.stop()
        except CandidateValidationError as error:
            result = {"status": "INVALID_CONFIG", "stage": "validation", "reason_code": error.reason_code}
        except PanelAPIError as error:
            result = {"status": "API_ERROR", "stage": "panel_api", "error_type": type(error).__name__, "error_message": str(error)}
        except (XrayClientError, FileNotFoundError, OSError) as error:
            result = {"status": "XRAY_ERROR", "stage": "xray_client", "error_type": type(error).__name__, "error_message": str(error)}
        except asyncio.TimeoutError as error:
            result = {"status": "TIMEOUT", "stage": "run", "error_type": type(error).__name__, "error_message": str(error)}
        except ValueError as error:
            result = {"status": "INVALID_CONFIG", "stage": "run", "error_type": type(error).__name__, "error_message": str(error)}
        except Exception as error:
            result = {"status": "FAILED", "stage": "run", "error_type": type(error).__name__, "error_message": str(error)}
        if result.get("status") != "OK":
            if client is not None:
                result["client_xray_diagnostics"] = client.diagnostic_categories()
            try:
                server_log = await self.panel.xray_logs(count=20) if hasattr(self.panel, "xray_logs") else None
            except Exception:
                server_log = None
            result["server_xray_log_status"] = "available" if server_log else "unavailable"
            result["server_xray_diagnostics"] = categorize_xray_logs(server_log)
        result = self._redact_result(result)
        return {"test_id": test_id, "run": run_number, "timestamp": started, "configuration_hash": digest,
                "configuration": self._public_values(values), "result": result}

    def _screening_gate(self, metrics: dict[str, Any]) -> tuple[bool, list[str]]:
        settings = dict(self.config.testing.get("screening", {}))
        failures: list[str] = []
        if (settings.get("require_tcp_connect", True)
                and metrics.get("tcp_connect_supported", True)
                and not metrics.get("tcp_connect_ok", False)):
            failures.append("tcp_connect")
        minimum = self._rate(settings.get("min_success_rate", 1.0), "screening.min_success_rate")
        if float(metrics.get("success_rate", 0.0)) < minimum:
            failures.append(f"success_rate<{minimum:g}")
        return not failures, failures

    @staticmethod
    def _transport_diagnostic(transport: str, metrics: dict[str, Any]) -> str | None:
        error_type = str(metrics.get("last_http_error", "")).partition(":")[0]
        if transport == "kcp" and error_type in {"ConnectTimeout", "ReadTimeout"}:
            return ("KCP/mKCP использует UDP; Xray не получил ответ. Проверьте доступность UDP-порта inbound "
                    "и совпадение kcpSettings на сервере и клиенте.")
        return None

    def _quality_gate(self, metrics: dict[str, Any]) -> tuple[bool, list[str], float]:
        settings = dict(self.config.testing.get("quality_gates", {}))
        failures: list[str] = []
        if (settings.get("require_tcp_connect", True)
                and metrics.get("tcp_connect_supported", True)
                and not metrics.get("tcp_connect_ok", False)):
            failures.append("tcp_connect")
        minimum_rate = self._rate(settings.get("min_success_rate", 1.0), "quality_gates.min_success_rate")
        success_rate = float(metrics.get("success_rate", 0.0))
        if success_rate < minimum_rate:
            failures.append(f"success_rate<{minimum_rate:g}")
        minimum_requests = max(1, int(settings.get("min_successful_requests", 1)))
        if int(metrics.get("successful_requests", 0)) < minimum_requests:
            failures.append(f"successful_requests<{minimum_requests}")
        maximum_p95 = float(settings.get("max_latency_p95_ms", 0) or 0)
        p95 = metrics.get("latency_p95_ms")
        if maximum_p95 > 0 and (p95 is None or float(p95) > maximum_p95):
            failures.append(f"latency_p95_ms>{maximum_p95:g}")
        minimum_speed = float(settings.get("min_download_mbps", 0) or 0)
        speed = metrics.get("download_mbps")
        if minimum_speed > 0 and (speed is None or float(speed) < minimum_speed):
            failures.append(f"download_mbps<{minimum_speed:g}")
        latency_score = 1.0 / (1.0 + float(p95 or 10_000) / 250.0)
        score = .75 * success_rate + .25 * latency_score
        if speed is not None:
            score = min(1.0, score * .9 + min(float(speed) / 100.0, 1.0) * .1)
        return not failures, failures, round(score, 6)

    @staticmethod
    def _rate(value: Any, name: str) -> float:
        rate = float(value)
        if not 0 <= rate <= 1:
            raise ValueError(f"testing.{name} must be between 0 and 1")
        return rate

    def _mutation_pool(self, parents: list[tuple[Combination, frozenset[str], float]],
                       tested: set[str], coverage: Counter[str],
                       children_per_parent: int | None) -> list[SearchCandidate]:
        unique: dict[str, SearchCandidate] = {}
        for parent, parent_changes, parent_score in parents:
            candidates = [SearchCandidate(child, changes, changes - parent_changes, parent_score)
                          for child, changes in self.generator.mutation_neighbors(parent.values, parent_changes)
                          if child.hash not in tested]
            if children_per_parent is not None:
                candidates = self._select_candidates(candidates, children_per_parent, coverage)
            for candidate in candidates:
                existing = unique.get(candidate.combination.hash)
                if existing is None or candidate.parent_score > existing.parent_score:
                    unique[candidate.combination.hash] = candidate
        return list(unique.values())

    @staticmethod
    def _select_candidates(candidates: list[SearchCandidate], limit: int,
                           coverage: Counter[str]) -> list[SearchCandidate]:
        pending = list(candidates)
        selected: list[SearchCandidate] = []
        planned = Counter(coverage)
        while pending and len(selected) < limit:
            candidate = min(
                pending,
                key=lambda item: (
                    sum(planned[name] for name in item.new_changes),
                    -item.parent_score,
                    item.combination.hash,
                ),
            )
            pending.remove(candidate)
            selected.append(candidate)
            planned.update(candidate.new_changes)
        return selected

    @staticmethod
    def _select_beam(nodes: list[tuple[Combination, frozenset[str], float]],
                     width: int) -> list[tuple[Combination, frozenset[str], float]]:
        pending = list(nodes)
        selected: list[tuple[Combination, frozenset[str], float]] = []
        while pending and len(selected) < width:
            def utility(node: tuple[Combination, frozenset[str], float]) -> tuple[float, str]:
                if not selected:
                    diversity = 1.0
                else:
                    distances = []
                    for _, existing, _ in selected:
                        union = node[1] | existing
                        distances.append(1.0 - len(node[1] & existing) / len(union) if union else 0.0)
                    diversity = min(distances)
                return node[2] + .1 * diversity, node[0].hash
            best = max(pending, key=utility)
            pending.remove(best)
            selected.append(best)
        return selected

    def _payload_hash(self, base_payload: dict[str, Any], values: dict[str, Any]) -> str:
        return configuration_hash(apply_mapping(base_payload, values, self.config.parameters))

    def _search_signature(self) -> str:
        parameters = [{"name": item.name, "target": item.target, "path": list(item.path),
                       "values": list(item.iter_values()), "conditions": item.conditions,
                       "value_conditions": list(item.value_conditions), "baseline": item.baseline,
                       "baseline_defined": item.baseline_defined, "mutate": item.mutate}
                      for item in self.config.parameters]
        testing = {key: self.config.testing.get(key) for key in (
            "combination_strategy", "mutation_generations", "beam_width", "children_per_parent",
            "runs_per_combination", "max_failed_runs", "screening", "quality_gates", "max_combinations",
        )}
        if "reality" in self.config.testing:
            testing["reality"] = self.config.testing["reality"]
        if self._reality_profile:
            testing["resolved_reality"] = {"target": self._reality_profile.target,
                                           "server_names": self._reality_profile.server_names}
        return configuration_hash({"parameters": parameters, "testing": testing})
