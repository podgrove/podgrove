"""Bound startup-only Pod replacement to the original controller and data volume."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time

from .errors import PodgroveError
from .kube import ENVIRONMENT, MANAGED, Kube, engine_pod_name


def _digest(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _timeout(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        raise PodgroveError("Startup recovery timeout must be finite and positive")
    return value


def _remaining(deadline, cancel):
    if cancel.is_set():
        raise PodgroveError("Startup engine recovery cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PodgroveError("Startup engine recovery deadline expired")
    return remaining


def _read(kube, kind, name, ident, deadline, cancel):
    result = kube.call("get", kind, name, "--ignore-not-found", "-o", "json",
                       check=False, timeout=min(15, _remaining(deadline, cancel)), cancel_event=cancel)
    if result.returncode:
        raise PodgroveError("Startup engine ownership could not be verified; no startup operation was retried")
    try:
        value = json.loads(result.stdout) if result.stdout.strip() else None
        if value is None:
            return None
        metadata = value["metadata"]
        labels = metadata["labels"]
        if (metadata["name"] != name or metadata["namespace"] != kube.namespace
                or not isinstance(metadata["uid"], str) or not metadata["uid"]
                or labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != ident):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PodgroveError("Startup engine resource is foreign or malformed; refusing recovery") from exc
    _remaining(deadline, cancel)
    return value


def capture_anchor(kube, ident: str, timeout: float = 15, *, cancel_event=None) -> dict:
    """Capture immutable controller/storage identity before a startup is spawned."""
    if not re.fullmatch(r"[a-f0-9]{12}", ident):
        raise PodgroveError("Invalid startup worktree identity")
    deadline = time.monotonic() + _timeout(timeout)
    cancel = cancel_event if cancel_event is not None else threading.Event()
    resources = [_read(kube, kind, "pg-" + ident, ident, deadline, cancel) for kind in ("statefulset", "pvc")]
    if any(not item or item["metadata"].get("deletionTimestamp") for item in resources):
        raise PodgroveError("Startup controller or storage is missing or deleting")
    controller, pvc = resources
    if not isinstance(controller.get("spec"), dict):
        raise PodgroveError("Startup controller specification is missing")
    return {"identity": ident, "context": kube.context, "namespace": kube.namespace,
            "statefulset_uid": controller["metadata"]["uid"], "pvc_uid": pvc["metadata"]["uid"],
            "controller_spec_sha256": _digest(controller["spec"])}


class StartupRecovery:
    """Permit a ready owned Pod only while its controller and PVC remain unchanged."""

    def __init__(self, kube, ident: str, anchor: dict, timeout: float, *, cancel_event=None):
        self.kube, self.ident = kube, ident
        self.anchor = dict(anchor)
        self.deadline = time.monotonic() + _timeout(timeout)
        self.cancel = cancel_event if cancel_event is not None else threading.Event()
        expected = {"identity", "context", "namespace", "statefulset_uid", "pvc_uid", "controller_spec_sha256"}
        if (set(anchor) != expected or anchor.get("identity") != ident or anchor.get("context") != kube.context
                or anchor.get("namespace") != kube.namespace
                or any(not isinstance(anchor.get(k), str) or not anchor[k] for k in expected)
                or not re.fullmatch(r"[a-f0-9]{64}", anchor["controller_spec_sha256"])):
            raise PodgroveError("Saved startup engine anchor is invalid")

    def remaining(self):
        return _remaining(self.deadline, self.cancel)

    def _observe(self):
        controller = _read(self.kube, "statefulset", "pg-" + self.ident, self.ident, self.deadline, self.cancel)
        pvc = _read(self.kube, "pvc", "pg-" + self.ident, self.ident, self.deadline, self.cancel)
        if (not controller or not pvc or controller["metadata"].get("deletionTimestamp")
                or pvc["metadata"].get("deletionTimestamp")
                or controller["metadata"]["uid"] != self.anchor["statefulset_uid"]
                or pvc["metadata"]["uid"] != self.anchor["pvc_uid"]
                or _digest(controller.get("spec")) != self.anchor["controller_spec_sha256"]):
            raise PodgroveError("Startup controller, storage or engine settings changed; refusing replay")
        pod = _read(self.kube, "pod", engine_pod_name(self.ident), self.ident, self.deadline, self.cancel)
        if not pod:
            return None
        Kube._validate_pod_controller(pod, controller, self.ident)
        claims = [volume["persistentVolumeClaim"].get("claimName")
                  for volume in pod.get("spec", {}).get("volumes", []) if "persistentVolumeClaim" in volume]
        if claims != ["pg-" + self.ident]:
            raise PodgroveError("Replacement startup Pod does not use the original owned volume")
        if pod["metadata"].get("deletionTimestamp"):
            return None
        status = pod.get("status", {})
        ready = status.get("phase") == "Running" and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions", []))
        return pod["metadata"]["uid"] if ready else None

    def wait(self) -> str:
        """Require two matching ready observations under one absolute deadline."""
        previous = None
        while True:
            self.remaining()
            try:
                observed = self._observe()
            except (TypeError, KeyError, AttributeError) as exc:
                raise PodgroveError("Startup engine resource is malformed; refusing recovery") from exc
            self.remaining()
            if observed is not None and observed == previous:
                return observed
            previous = observed
            self.cancel.wait(min(0.25, self.remaining()))
