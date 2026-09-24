from __future__ import annotations

import re
from typing import Any, Iterable

_SECRET_KEY = re.compile(r"(password|token|secret|private.?key|public.?key|short.?id|credential|cookie|authorization)", re.I)


def mask_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "***" if _SECRET_KEY.search(str(key)) else mask_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1***", value)
    return value


def mask_parameter_values(values: dict[str, Any], parameters: Iterable[Any]) -> dict[str, Any]:
    """Also hide values mapped to a secret path under an innocuous parameter name."""
    by_name = {parameter.name: parameter for parameter in parameters}
    public: dict[str, Any] = {}
    for name, value in values.items():
        parameter = by_name.get(name)
        path = ".".join(map(str, parameter.path)) if parameter is not None else ""
        public[name] = "***" if _SECRET_KEY.search(name + "." + path) else mask_secrets(value)
    return public
