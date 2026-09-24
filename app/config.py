from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.parameters.models import ParameterSpec

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda match: os.environ.get(match.group(1), match.group(0)), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class ExperimentConfig:
    panel: dict[str, Any]
    inbound: dict[str, Any]
    parameters: tuple[ParameterSpec, ...]
    testing: dict[str, Any] = field(default_factory=dict)
    timeouts: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    @property
    def output_dir(self) -> Path:
        return Path(self.output.get("directory", "./results"))

    @property
    def runs_per_combination(self) -> int:
        value = int(self.testing.get("runs_per_combination", 1))
        if value < 1:
            raise ValueError("testing.runs_per_combination must be at least 1")
        return value

    @property
    def max_failed_runs(self) -> int:
        """Maximum failed attempts for one configuration before it is abandoned."""
        value = int(self.testing.get("max_failed_runs", 1))
        if value < 1:
            raise ValueError("testing.max_failed_runs must be at least 1")
        return value

    @property
    def beam_width(self) -> int:
        value = int(self.testing.get("beam_width", 8))
        if value < 1:
            raise ValueError("testing.beam_width must be at least 1")
        return value

    @property
    def children_per_parent(self) -> int:
        value = int(self.testing.get("children_per_parent", 8))
        if value < 1:
            raise ValueError("testing.children_per_parent must be at least 1")
        return value

    @property
    def two_stage(self) -> dict[str, Any]:
        value = self.testing.get("two_stage", {})
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("testing.two_stage must be an object")
        return value

    @property
    def two_stage_enabled(self) -> bool:
        return bool(self.two_stage.get("enabled", False))

    @property
    def validation_top_k(self) -> int:
        value = int(self.two_stage.get("top_k", 20))
        if value < 1:
            raise ValueError("testing.two_stage.top_k must be at least 1")
        return value

    @property
    def validation_runs(self) -> int:
        value = int(self.two_stage.get("validation_runs", 5))
        if value < 1:
            raise ValueError("testing.two_stage.validation_runs must be at least 1")
        return value

    def maximum_run_count(self, search_configurations: int) -> int:
        validation = self.validation_top_k * self.validation_runs if self.two_stage_enabled else 0
        return search_configurations * self.runs_per_combination + validation

    @property
    def api_timeout(self) -> float:
        return float(self.timeouts.get("api", 10))

    @property
    def combination_strategy(self) -> str:
        strategy = str(self.testing.get("combination_strategy", "pairwise"))
        if strategy not in {"mutation", "pairwise", "exhaustive"}:
            raise ValueError("testing.combination_strategy must be mutation, pairwise or exhaustive")
        return strategy

    @property
    def max_combinations(self) -> int:
        value = int(self.testing.get("max_combinations", 500))
        if value < 1:
            raise ValueError("testing.max_combinations must be at least 1")
        return value

    @property
    def mutation_generations(self) -> int:
        value = int(self.testing.get("mutation_generations", 2))
        if value < 1:
            raise ValueError("testing.mutation_generations must be at least 1")
        return value


def load_config(path: Path) -> ExperimentConfig:
    raw = _expand_env(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    for section in ("panel", "inbound", "parameters"):
        if section not in raw:
            raise ValueError(f"Missing required configuration section: {section}")
    params = tuple(ParameterSpec.from_dict(name, definition) for name, definition in raw["parameters"].items())
    names = [parameter.name for parameter in params]
    if len(names) != len(set(names)):
        raise ValueError("Parameter names must be unique")
    return ExperimentConfig(
        panel=raw["panel"], inbound=raw["inbound"], parameters=params,
        testing=raw.get("testing", {}), timeouts=raw.get("timeouts", {}),
        output=raw.get("output", {}), source_path=path.resolve(),
    )


def offline_warnings(config: ExperimentConfig) -> list[str]:
    """Warn about YAML-only issues without assuming access to the source inbound."""
    warnings: list[str] = []
    reality_selected = any(
        parameter.target == "inbound"
        and parameter.path == ("streamSettings", "security")
        and ("reality" in tuple(parameter.iter_values())
             or parameter.baseline_defined and parameter.baseline == "reality")
        for parameter in config.parameters
    )
    if reality_selected:
        reality = config.testing.get("reality", {})
        if not isinstance(reality, dict) or not reality.get("target") or not reality.get("server_names"):
            warnings.append(
                "REALITY is selected, but testing.reality.target/server_names are incomplete; "
                "candidates may be skipped if the source inbound has no REALITY settings."
            )
    request_timeout = float(config.timeouts.get("request", 5))
    connect_timeout = float(config.timeouts.get("tcp_connect", min(request_timeout, 3)))
    screening_timeout = float(config.timeouts.get("screening", 8))
    requests = max(1, int((config.testing.get("screening") or {}).get("requests", 1)))
    if screening_timeout <= connect_timeout + requests * request_timeout:
        warnings.append("Screening timeout may be too short for TCP connect plus HTTP requests.")
    if config.max_failed_runs < config.runs_per_combination:
        warnings.append("max_failed_runs may stop a candidate before all planned repeats finish.")
    return warnings
