"""Project recorded endpoints without confusing container and tunnel readiness."""
from __future__ import annotations

import time


def observed(data: dict, *, connected: bool | None = None, ping: dict | None = None) -> dict:
    result = dict(data)
    if ping is not None and (not isinstance(ping, dict) or ping.get("ok") is not True):
        connected, ping = False, None
    if isinstance(ping, dict):
        if ping.get("ok") is True and ping.get("status") in ("starting", "ready", "degraded", "unhealthy", "error", "disconnected"):
            result["status"] = ping["status"]
        for key in ("forward_status", "sync_status"):
            if isinstance(ping.get(key), dict):
                result[key] = dict(ping[key])
    forward = result.get("forward_status")
    forward = dict(forward) if isinstance(forward, dict) else {}
    current = forward.get("state", "unknown")
    if current not in ("ready", "reconnecting", "disconnected", "disabled"):
        current = "unknown"
    if connected is False or result.get("status") in ("disconnected", "reaped", "error"):
        current = "disconnected"
        if result.get("status") not in ("error", "reaped", "starting"):
            result["status"] = "disconnected"
    elif connected is None:
        checked = forward.get("checked_at")
        if not isinstance(checked, (float, int)) or not -5 <= time.time() - checked <= 45:
            current = "unknown"
    if not data.get("ports"):
        current = "disabled"
    forward["state"] = current
    result["forward_status"] = forward
    result["ports"] = [dict(port, status=current) for port in data.get("ports", []) if isinstance(port, dict)]
    retrying = isinstance(result.get("sync_status"), dict) and result["sync_status"].get("state") == "retrying"
    if result.get("status") == "ready" and (current not in ("ready", "disabled") or retrying):
        result["status"] = "degraded"
    return result
