"""Bounded Kubernetes node placement without node inventory access."""
from __future__ import annotations

import copy
import re

import jsonschema

from .errors import PodgroveError

_LABEL_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?"
_LABEL_VALUE = {"type": "string", "maxLength": 63, "pattern": rf"^(?:{_LABEL_NAME})?$"}
_REQUIREMENT = {
    "type": "object", "additionalProperties": False, "required": ["key", "operator"],
    "properties": {
        "key": {"type": "string", "minLength": 1, "maxLength": 317},
        "operator": {"enum": ["In", "NotIn", "Exists", "DoesNotExist", "Gt", "Lt"]},
        "values": {"type": "array", "maxItems": 64, "uniqueItems": True,
                   "items": {"type": "string", "maxLength": 63}},
    },
}
_TERM = {
    "type": "object", "additionalProperties": False, "required": ["matchExpressions"],
    "properties": {"matchExpressions": {"type": "array", "minItems": 1, "maxItems": 32,
                                          "uniqueItems": True, "items": _REQUIREMENT}},
}
PLACEMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "nodeSelector": {"type": "object", "maxProperties": 64, "additionalProperties": _LABEL_VALUE},
        "affinity": {
            "type": "object", "additionalProperties": False,
            "properties": {"nodeAffinity": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "type": "object", "additionalProperties": False, "required": ["nodeSelectorTerms"],
                        "properties": {"nodeSelectorTerms": {"type": "array", "minItems": 1, "maxItems": 16,
                                                               "uniqueItems": True, "items": _TERM}},
                    },
                    "preferredDuringSchedulingIgnoredDuringExecution": {
                        "type": "array", "maxItems": 16, "uniqueItems": True,
                        "items": {"type": "object", "additionalProperties": False,
                                  "required": ["weight", "preference"],
                                  "properties": {"weight": {"type": "integer", "minimum": 1, "maximum": 100},
                                                 "preference": _TERM}},
                    },
                },
            }},
        },
        "tolerations": {
            "type": "array", "maxItems": 64, "uniqueItems": True,
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {
                          "key": {"type": "string", "maxLength": 317},
                          "operator": {"enum": ["Equal", "Exists"]},
                          "value": _LABEL_VALUE,
                          "effect": {"enum": ["NoSchedule", "PreferNoSchedule", "NoExecute"]},
                          "tolerationSeconds": {"type": "integer", "minimum": 0, "maximum": 2147483647},
                      }},
        },
    },
}
_COMPUTE_GUARD = {"key": "eks.amazonaws.com/compute-type", "operator": "NotIn", "values": ["fargate", "auto"]}


def _label_key(key: str) -> bool:
    if not isinstance(key, str):
        return False
    parts = key.split("/")
    if not re.fullmatch(_LABEL_NAME, parts[-1]):
        return False
    return len(parts) == 1 or (len(parts) == 2 and len(parts[0]) <= 253 and all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in parts[0].split(".")))


def _requirements(term: dict) -> None:
    for requirement in term["matchExpressions"]:
        key, operator, values = requirement["key"], requirement["operator"], requirement.get("values", [])
        if not _label_key(key):
            raise PodgroveError("placement.affinity: invalid Kubernetes label key")
        if operator in ("In", "NotIn") and any(not re.fullmatch(rf"(?:{_LABEL_NAME})?", value) for value in values):
            raise PodgroveError("placement.affinity: In and NotIn require Kubernetes label values")
        if operator in ("In", "NotIn") and not values:
            raise PodgroveError("placement.affinity: In and NotIn require nonempty values")
        if operator in ("Exists", "DoesNotExist") and values:
            raise PodgroveError("placement.affinity: Exists and DoesNotExist do not accept values")
        if operator in ("Gt", "Lt") and (len(values) != 1 or not re.fullmatch(r"-?[0-9]+", values[0])
                                          or not -(2**63) <= int(values[0]) < 2**63):
            raise PodgroveError("placement.affinity: Gt and Lt require one signed 64-bit integer string")


def validate_placement(value: dict | None) -> dict:
    value = {} if value is None else value
    errors = sorted(jsonschema.Draft202012Validator(PLACEMENT_SCHEMA).iter_errors(value), key=lambda e: str(e.path))
    if errors:
        raise PodgroveError(f"placement: {errors[0].message}")
    result = copy.deepcopy(value)
    if any(not _label_key(key) for key in result.get("nodeSelector", {})):
        raise PodgroveError("placement.nodeSelector: invalid Kubernetes label key")
    affinity = result.get("affinity", {}).get("nodeAffinity", {})
    for term in affinity.get("requiredDuringSchedulingIgnoredDuringExecution", {}).get("nodeSelectorTerms", []):
        _requirements(term)
    for item in affinity.get("preferredDuringSchedulingIgnoredDuringExecution", []):
        _requirements(item["preference"])
    normalized = []
    for toleration in result.get("tolerations", []):
        key, operator = toleration.get("key", ""), toleration.get("operator", "Equal")
        if key and not _label_key(key):
            raise PodgroveError("placement.tolerations: invalid Kubernetes taint key")
        if not key and operator != "Exists":
            raise PodgroveError("placement.tolerations: an empty key requires operator Exists")
        if operator == "Exists" and toleration.get("value", ""):
            raise PodgroveError("placement.tolerations: Exists cannot specify a value")
        if "tolerationSeconds" in toleration and toleration.get("effect") != "NoExecute":
            raise PodgroveError("placement.tolerations: tolerationSeconds requires effect NoExecute")
        item = {"key": key, "operator": operator, "value": toleration.get("value", ""), **toleration}
        if item in normalized:
            raise PodgroveError("placement.tolerations: duplicate equivalent toleration")
        normalized.append(item)
    return result


def placement_spec(value: dict | None, *, node_mode: str, tainted_nodes: dict) -> dict:
    requested = validate_placement(value)
    selector = {"kubernetes.io/os": "linux", **(tainted_nodes["selector"] if node_mode == "tainted" else {})}
    for key, item in requested.get("nodeSelector", {}).items():
        if key in selector and selector[key] != item:
            raise PodgroveError(f"placement.nodeSelector: conflicts with required node selector {key}")
        selector[key] = item
    if selector.get("eks.amazonaws.com/compute-type") in ("fargate", "auto"):
        raise PodgroveError("placement.nodeSelector: the Docker engine requires supported Linux nodes, not Fargate or Auto")
    affinity = copy.deepcopy(requested.get("affinity", {}).get("nodeAffinity", {}))
    terms = affinity.get("requiredDuringSchedulingIgnoredDuringExecution", {}).get("nodeSelectorTerms", [])
    for term in terms:
        for requirement in term["matchExpressions"]:
            key, operator, values = requirement["key"], requirement["operator"], requirement.get("values", [])
            if key in selector:
                selected = selector[key]
                contradiction = ((operator == "In" and selected not in values)
                                 or (operator == "NotIn" and selected in values) or operator == "DoesNotExist")
                if operator in ("Gt", "Lt"):
                    contradiction = not re.fullmatch(r"-?[0-9]+", selected) or not (
                        int(selected) > int(values[0]) if operator == "Gt" else int(selected) < int(values[0]))
                if contradiction:
                    raise PodgroveError(f"placement.affinity: conflicts with required node selector {key}")
            if key == "eks.amazonaws.com/compute-type" and operator == "In" and set(values) <= {"fargate", "auto"}:
                raise PodgroveError("placement.affinity: required term selects only unsupported Fargate or Auto nodes")
        term["matchExpressions"].append(copy.deepcopy(_COMPUTE_GUARD))
    affinity["requiredDuringSchedulingIgnoredDuringExecution"] = {
        "nodeSelectorTerms": terms or [{"matchExpressions": [copy.deepcopy(_COMPUTE_GUARD)]}],
    }
    tolerations = [{"operator": "Equal", **tainted_nodes["taint"]}] if node_mode == "tainted" else []
    for item in requested.get("tolerations", []):
        if not any({"operator": "Equal", **existing} == {"operator": "Equal", **item} for existing in tolerations):
            tolerations.append(copy.deepcopy(item))
    return {"nodeSelector": selector, "affinity": {"nodeAffinity": affinity}, "tolerations": tolerations}
