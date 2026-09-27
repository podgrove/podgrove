"""Explicit Kubernetes resource budgets and quantity validation.

Custom budgets replace presets; they never silently inherit a missing limit.
Cluster admission still controls defaulting, quotas and acceptable allocations.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, DecimalException, localcontext
import math
import re

from .errors import PodgroveError

RESOURCE_NAMES = ("cpu", "memory", "ephemeral-storage")
QUANTITY_PATTERN = r"^[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[EPTGMK]i|[EPTGMkmun]|[eE][+-]?[0-9]+)?$"
QUANTITY_SCHEMA = {"anyOf": [
    {"type": "string", "minLength": 1, "maxLength": 128, "pattern": QUANTITY_PATTERN},
    {"type": "number", "exclusiveMinimum": 0},
]}
RESOURCE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    group: {"type": "object", "additionalProperties": False,
            "properties": {name: {"anyOf": [deepcopy(QUANTITY_SCHEMA["anyOf"][0]),
                                             {"type": "number", "minimum": 0}]} for name in RESOURCE_NAMES}}
    for group in ("requests", "limits")
}}
SIZES = {
    "small": ({"cpu": "250m", "memory": "2Gi"}, {"cpu": "2", "memory": "2Gi"}),
    "medium": ({"cpu": "1", "memory": "8Gi"}, {"cpu": "4", "memory": "8Gi"}),
    "large": ({"cpu": "2", "memory": "16Gi"}, {"cpu": "8", "memory": "16Gi"}),
}
INIT_RESOURCES = {"requests": {"cpu": "10m", "memory": "16Mi"},
                  "limits": {"cpu": "100m", "memory": "32Mi"}}


def quantity(value, field: str = "quantity", *, allow_zero: bool = False) -> Decimal:
    """Parse a Kubernetes quantity without floating-point comparison.

    The magnitude bound is Kubernetes Quantity's signed-64-bit representation,
    not an engine-size ceiling. Reject pathological inputs before arithmetic.
    """
    adjective = "nonnegative" if allow_zero else "positive"
    if (isinstance(value, bool) or not isinstance(value, (str, int, float))
            or (isinstance(value, float) and not math.isfinite(value))):
        raise PodgroveError(f"{field}: expected a {adjective} Kubernetes quantity")
    try:
        text = str(value)
    except ValueError as exc:
        raise PodgroveError(f"{field}: quantity is not representable by Kubernetes") from exc
    if len(text) > 128 or not re.fullmatch(QUANTITY_PATTERN, text):
        raise PodgroveError(f"{field}: expected a {adjective} Kubernetes quantity, such as 500m, 2 or 4Gi")
    match = re.fullmatch(r"([+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))([EPTGMK]i|[EPTGMkmun]|[eE][+-]?[0-9]+)?", text)
    number, suffix = match.groups()
    suffix = suffix or ""
    try:
        with localcontext() as ctx:
            ctx.prec = 256
            if suffix.endswith("i"):
                result = Decimal(number) * (Decimal(1024) ** ("KMGTPE".index(suffix[0]) + 1))
            elif suffix.startswith(("e", "E")) and len(suffix) > 1:
                # Kubernetes quantities below nanounit precision are rounded;
                # require an exactly representable positive quantity instead.
                exponent = int(suffix[1:])
                if abs(exponent) > 256:
                    raise ValueError
                result = Decimal(number).scaleb(exponent)
            else:
                result = Decimal(number).scaleb({"": 0, "n": -9, "u": -6, "m": -3, "k": 3,
                                                "M": 6, "G": 9, "T": 12, "P": 15, "E": 18}[suffix])
            if (result < 0 or (result == 0 and not allow_zero) or result > Decimal(2**63 - 1)
                    or (result != 0 and result.normalize().as_tuple().exponent < -9)):
                raise ValueError
            if field.rsplit(".", 1)[-1] == "cpu" and result.normalize().as_tuple().exponent < -3:
                raise PodgroveError(f"{field}: CPU precision must be a whole millicore (1m / 0.001 CPU)")
            return result
    except (ValueError, KeyError, DecimalException) as exc:
        raise PodgroveError(f"{field}: quantity must be {adjective} and exactly representable by Kubernetes") from exc


def quantity_text(value, field: str, *, allow_zero: bool = False) -> str:
    quantity(value, field, allow_zero=allow_zero)
    return str(value)


def resource_budget(value: dict, field: str = "resources") -> dict[str, dict[str, str]]:
    """Normalize only declared fields. Missing groups and dimensions stay absent."""
    if not isinstance(value, dict) or set(value) - {"requests", "limits"}:
        raise PodgroveError(f"{field}: expected only requests and limits")
    result = {}
    for group, values in value.items():
        if not isinstance(values, dict) or set(values) - set(RESOURCE_NAMES):
            raise PodgroveError(f"{field}.{group}: expected only cpu, memory and ephemeral-storage")
        if values:
            result[group] = {name: quantity_text(amount, f"{field}.{group}.{name}", allow_zero=True)
                             for name, amount in values.items()}
    for name, requested in result.get("requests", {}).items():
        maximum = result.get("limits", {}).get(name)
        if maximum is not None and quantity(requested, allow_zero=True) > quantity(maximum, allow_zero=True):
            raise PodgroveError(f"{field}.requests.{name}: request must not exceed its limit")
    return result


def engine_resources(size: str, custom: dict | None = None) -> dict[str, dict[str, str]]:
    if custom is not None:
        return resource_budget(custom)
    if size not in SIZES:
        raise PodgroveError("size: expected small, medium or large")
    requests, limits = SIZES[size]
    return {"requests": dict(requests), "limits": {**limits, "ephemeral-storage": "4Gi"}}


def initializer_resources(custom: dict | None = None) -> dict[str, dict[str, str]]:
    return deepcopy(INIT_RESOURCES) if custom is None else resource_budget(custom, "init_resources")


def same_resources(wanted: dict, actual: dict) -> bool:
    """Compare quantities and Kubernetes' request-from-limit default exactly.

    Missing custom limits must not silently accept a previous preset's caps.
    Extra admission-injected constraints require explicit configuration review.
    """
    def normalized(budget):
        clean = resource_budget(budget)
        limits = clean.get("limits", {})
        requests = {**limits, **clean.get("requests", {})}
        return {group: {name: quantity(value, allow_zero=True) for name, value in values.items()}
                for group, values in (("requests", requests), ("limits", limits)) if values}
    try:
        return normalized(wanted) == normalized(actual)
    except PodgroveError:
        return False
