"""Fail-closed TTL and explicit GitLab merge-request lifecycle checks."""
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import state
from .errors import PodgroveError
from .kube import ENVIRONMENT, MANAGED, Kube, engine_pod_name


def mr_endpoint(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    match = re.fullmatch(r"/(.+)/-/merge_requests/([1-9][0-9]*)/?", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "gitlab.com" or not match or parsed.query or parsed.fragment:
        raise PodgroveError("--mr-url must be https://gitlab.com/<project>/-/merge_requests/<number>")
    project = urllib.parse.quote(match.group(1), safe="")
    return f"https://gitlab.com/api/v4/projects/{project}/merge_requests/{match.group(2)}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PodgroveError("GitLab redirected the lifecycle request; refusing to forward credentials")


def mr_closed(url: str) -> bool:
    headers = {"Accept": "application/json"}
    token = os.environ.get("PODGROVE_GITLAB_TOKEN")
    if token:
        headers["PRIVATE-TOKEN"] = token
    request = urllib.request.Request(mr_endpoint(url), headers=headers)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            data = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PodgroveError("Could not verify merge-request state; leaving environment in place") from exc
    if not isinstance(data, dict) or data.get("state") not in ("opened", "locked", "merged", "closed"):
        raise PodgroveError("Could not verify merge-request state: invalid GitLab response; leaving environment in place")
    return data.get("state") in ("merged", "closed")


def reason(data: dict, now: float | None = None, *, check_mr=True) -> str | None:
    now = time.time() if now is None else now
    try:
        last, ttl = float(data["last_activity"]), float(data["ttl_seconds"])
    except (KeyError, ValueError, TypeError) as exc:
        raise PodgroveError("Invalid lifecycle metadata; refusing cleanup") from exc
    if not math.isfinite(ttl) or ttl <= 0 or not (0 < last <= now + 60):
        raise PodgroveError("Invalid lifecycle clock or TTL; refusing cleanup")
    if now - last >= ttl:
        return "idle TTL expired"
    if check_mr and data.get("mr_url") and mr_closed(data["mr_url"]):
        return "merge request merged or closed"
    return None


def _local_record(kube: Kube, ident: str) -> dict | None:
    # A cluster reaper cannot infer that another laptop's worktree is gone.
    # Only this machine's validated, matching local record establishes that fact.
    if not isinstance(kube.context, str) or not isinstance(kube.namespace, str):
        return None
    matches = [{"path": path, "data": data} for path, data in state.list_states(kube.context)
               if data["identity"] == ident and data["namespace"] == kube.namespace]
    if len(matches) > 1:
        raise PodgroveError("Ambiguous local environment records; refusing cleanup")
    return matches[0] if matches else None


def _missing_worktree(record: dict | None) -> bool:
    if record is None:
        return False
    root = Path(record["data"]["root"])
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        # A missing mount/parent is not proof the developer deleted a worktree.
        # Require the immediate parent to remain an ordinary directory.
        try:
            return stat.S_ISDIR(root.parent.lstat().st_mode)
        except OSError:
            return False
    except OSError as exc:
        raise PodgroveError("Could not inspect local worktree; refusing orphan cleanup") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise PodgroveError("Local worktree was replaced by a non-directory; refusing orphan cleanup")
    return False


def _stop_local(record: dict) -> None:
    from .runtime import stop_session
    stop_session(record["data"])


def _remaining_without_lease(kube: Kube, ident: str) -> list[str]:
    """Inspect exact names only; foreign collisions are never treated as absence."""
    name = f"pg-{ident}"
    if kube.get("configmap", name):
        raise PodgroveError("An environment lease exists or appeared; retry reap before local cleanup")
    remaining = []
    for kind, resource_name in (("statefulset", name), ("pod", engine_pod_name(ident)), ("pod", name),
                                ("pvc", name), ("networkpolicy", name), ("service", name)):
        resource = kube.get(kind, resource_name)
        if not resource:
            continue
        metadata = resource.get("metadata", {})
        labels = metadata.get("labels", {})
        if (metadata.get("name") != resource_name or metadata.get("namespace") != kube.namespace
                or labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != ident):
            raise PodgroveError(f"Refusing local cleanup: {kind}/{resource_name} is foreign")
        remaining.append(f"{kind}/{resource_name}")
    return remaining


def _local_cleanup_reason(record: dict, remaining: list[str]) -> str:
    if not remaining:
        return "cluster environment already absent"
    if _missing_worktree(record):
        return "local worktree no longer exists"
    data = record["data"]
    lifecycle = {**data, "last_activity": data.get("last_activity", data.get("created_at"))}
    if reason(lifecycle, check_mr=False):
        return "idle TTL expired"
    raise PodgroveError("Environment lease is missing while owned resources remain; inspect the environment and run podgrove down")


def _reap_local_without_leases(kube: Kube, seen: set[str], dry_run: bool, identity: str | None) -> list[dict]:
    if not isinstance(kube.context, str) or not isinstance(kube.namespace, str):
        return []
    results = []
    for path, data in state.list_states(kube.context):
        ident = data["identity"]
        if data["namespace"] != kube.namespace or ident in seen or (identity is not None and ident != identity):
            continue
        try:
            record = {"path": path, "data": data}
            mode = state.namespace_mode(data)
            remaining = _remaining_without_lease(kube, ident)
            why = _local_cleanup_reason(record, remaining)
            if not dry_run:
                with state.lock(path):
                    latest = state.read(path)
                    state.validate_binding(latest, Path(data["root"]), kube.context)
                    if latest["namespace"] != kube.namespace:
                        raise PodgroveError("Local environment namespace changed; refusing cleanup")
                    if state.namespace_mode(latest) != mode:
                        raise PodgroveError("Local namespace_mode changed; refusing cleanup")
                    record = {"path": path, "data": latest}
                    remaining = _remaining_without_lease(kube, ident)
                    why = _local_cleanup_reason(record, remaining)
                    _stop_local(record)
                    if remaining:
                        kube.destroy(ident, namespace_mode=mode)
                    state.cleanup(path, latest)
            results.append({"identity": ident, "reason": why, "deleted": not dry_run})
        except PodgroveError as exc:
            results.append({"identity": ident, "error": str(exc), "deleted": False})
    return results


def reap(kube: Kube, dry_run: bool = False, *, identity: str | None = None) -> list[dict]:
    if identity is not None and not re.fullmatch(r"[a-f0-9]{12}", identity):
        raise PodgroveError("--environment must be a 12-character lowercase hexadecimal environment identity")
    selector = f"{MANAGED}=podgrove" + (f",{ENVIRONMENT}={identity}" if identity else "")
    leases = kube.get("configmap", selector=selector).get("items", [])
    results = []
    seen = set()
    for lease in leases:
        metadata = lease.get("metadata", {})
        labels = metadata.get("labels", {})
        ident = labels.get(ENVIRONMENT, "")
        if (labels.get(MANAGED) != "podgrove" or not re.fullmatch(r"[a-f0-9]{12}", ident)
                or (identity is not None and ident != identity)
                or metadata.get("name") != f"pg-{ident}"):
            continue
        seen.add(ident)
        try:
            mode = Kube.lease_mode(kube, ident, lease)
            local = _local_record(kube, ident)
            if local and state.namespace_mode(local["data"]) != mode:
                raise PodgroveError("Local and lease namespace_mode disagree; refusing cleanup")
            why = reason(lease.get("data", {}), check_mr=False)
            if not why and _missing_worktree(local):
                why = "local worktree no longer exists"
            if not why:
                why = reason(lease.get("data", {}))
            if why:
                if not dry_run:
                    # Re-read the lease immediately before deletion; activity may have advanced.
                    current = kube.get("configmap", f"pg-{ident}")
                    current_meta = current.get("metadata", {})
                    current_labels = current_meta.get("labels", {})
                    if (current_meta.get("resourceVersion") != metadata.get("resourceVersion")
                            or current_meta.get("name") != f"pg-{ident}"
                            or current_labels.get(MANAGED) != "podgrove"
                            or current_labels.get(ENVIRONMENT) != ident):
                        continue
                    if Kube.lease_mode(kube, ident, current) != mode:
                        raise PodgroveError("Environment lease namespace_mode changed; refusing cleanup")
                    if local:
                        with state.lock(local["path"]):
                            latest = state.read(local["path"])
                            state.validate_binding(latest, Path(local["data"]["root"]), kube.context)
                            if latest.get("namespace") != kube.namespace:
                                raise PodgroveError("Local environment namespace changed; refusing cleanup")
                            if state.namespace_mode(latest) != mode:
                                raise PodgroveError("Local and lease namespace_mode disagree; refusing cleanup")
                            local = {"path": local["path"], "data": latest}
                            if why == "local worktree no longer exists" and not _missing_worktree(local):
                                continue
                            _stop_local(local)
                            kube.destroy(ident, namespace_mode=mode)
                            state.cleanup(local["path"], latest)
                    else:
                        kube.destroy(ident, namespace_mode=mode)
                results.append({"identity": ident, "reason": why, "deleted": not dry_run})
        except PodgroveError as exc:
            results.append({"identity": ident, "error": str(exc), "deleted": False})
    results.extend(_reap_local_without_leases(kube, seen, dry_run, identity))
    return results
