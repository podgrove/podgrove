"""Fresh, cancellable ownership checks before replacing a file-sync receiver."""
from __future__ import annotations

import json
import math
import re
import subprocess
import time

from .errors import PodgroveError
from .kube import ENVIRONMENT, MANAGED

VERIFY_SECONDS = 15.0


class SyncRecoveryUnavailable(PodgroveError):
    """Ownership could not be observed; a later fresh read may be attempted."""


class SyncOwnershipError(PodgroveError):
    """The captured engine is missing, replaced, or no longer owned."""


def verify_engine(kube, ident: str, expected: dict, cancel_event, *, timeout=VERIFY_SECONDS) -> dict:
    """Read only the named controller/Pod, without adopting a replacement.

    Both reads share one deadline. Their explicit API timeout leaves a small
    cleanup allowance inside that budget; cancellation reaches the subprocess.
    Callers decide whether to retry an unavailable observation. No writes or
    command retries occur here.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise SyncRecoveryUnavailable("File sync ownership read has no usable time budget")
    keys = ("statefulset_uid", "pod_uid")
    if (not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{12}", ident)
            or not isinstance(expected, dict)
            or any(not isinstance(expected.get(key), str) or not expected[key].strip() for key in keys)):
        raise SyncOwnershipError("File sync recovery requires the original engine identity")
    captured = {key: expected[key] for key in keys}
    deadline = time.monotonic() + min(VERIFY_SECONDS, timeout)

    def remaining():
        if cancel_event.is_set():
            raise SyncRecoveryUnavailable("File sync recovery cancelled")
        left = deadline - time.monotonic()
        if left <= 0:
            raise SyncRecoveryUnavailable("File sync ownership read timed out")
        return left

    def resource(kind, name, uid):
        left = remaining()
        request_seconds = max(.001, left - min(.5, left / 2))
        try:
            result = kube.call("get", kind, name, "-o", "json", "--ignore-not-found",
                               f"--request-timeout={request_seconds:.6f}s", timeout=left,
                               check=False, cancel_event=cancel_event)
        except (PodgroveError, OSError, subprocess.TimeoutExpired) as exc:
            raise SyncRecoveryUnavailable("File sync ownership read is unavailable") from exc
        remaining()
        if result.returncode != 0:
            raise SyncRecoveryUnavailable("File sync ownership read is unavailable")
        try:
            value = json.loads(result.stdout) if result.stdout.strip() else {}
        except (ValueError, TypeError, AttributeError) as exc:
            raise SyncRecoveryUnavailable("File sync ownership response is incomplete") from exc
        metadata = value.get("metadata") if isinstance(value, dict) else None
        labels = metadata.get("labels") if isinstance(metadata, dict) else None
        if (not isinstance(metadata, dict) or not isinstance(labels, dict)
                or metadata.get("name") != name or metadata.get("namespace") != kube.namespace
                or metadata.get("deletionTimestamp") is not None
                or labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != ident
                or metadata.get("uid") != uid):
            raise SyncOwnershipError("File sync engine is missing, replaced, deleting, or no longer owned")
        return value

    name = f"pg-{ident}"
    resource("statefulset", name, captured["statefulset_uid"])
    pod = resource("pod", f"{name}-0", captured["pod_uid"])
    references = pod["metadata"].get("ownerReferences")
    if (not isinstance(references, list) or any(not isinstance(ref, dict) for ref in references)):
        raise SyncOwnershipError("File sync engine has an invalid controller reference")
    controllers = [ref for ref in references if ref.get("controller") is True]
    if (len(controllers) != 1 or controllers[0].get("apiVersion") != "apps/v1"
            or controllers[0].get("kind") != "StatefulSet" or controllers[0].get("name") != name
            or controllers[0].get("uid") != captured["statefulset_uid"]):
        raise SyncOwnershipError("File sync engine is not controlled by its original StatefulSet")
    remaining()
    return captured
