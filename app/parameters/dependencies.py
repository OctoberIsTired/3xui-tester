from __future__ import annotations

from typing import Any


def conditions_match(rule: dict[str, Any] | None, values: dict[str, Any]) -> bool:
    """Evaluate the declarative subset: equals, not_equals, in, not_in, exists, all, any."""
    if not rule:
        return True
    if "all" in rule:
        _require_list(rule["all"], "all")
        return all(conditions_match(item, values) for item in rule["all"])
    if "any" in rule:
        _require_list(rule["any"], "any")
        return any(conditions_match(item, values) for item in rule["any"])
    if "parameter" in rule:
        parameter = rule["parameter"]
        predicates = {key: value for key, value in rule.items() if key != "parameter"}
        return _predicates(parameter, predicates, values)
    # Shorthand: {network: {equals: ws}, security: {in: [tls, reality]}}
    return all(_predicates(name, predicates if isinstance(predicates, dict) else {"equals": predicates}, values)
               for name, predicates in rule.items())


def _require_list(value: Any, name: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"Dependency {name} must be a list")


def _predicates(name: str, predicates: dict[str, Any], values: dict[str, Any]) -> bool:
    actual = values.get(name)
    exists = name in values and actual is not None
    for operator, expected in predicates.items():
        if operator == "equals" and actual != expected:
            return False
        if operator == "not_equals" and actual == expected:
            return False
        if operator == "in" and (not isinstance(expected, list) or actual not in expected):
            return False
        if operator == "not_in" and (not isinstance(expected, list) or actual in expected):
            return False
        if operator == "exists" and exists != bool(expected):
            return False
        if operator not in {"equals", "not_equals", "in", "not_in", "exists"}:
            raise ValueError(f"Unsupported dependency operator: {operator}")
    return True
