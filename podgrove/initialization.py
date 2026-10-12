"""Bound a running storage initializer independently of application build time."""
from __future__ import annotations

import math
import time
from datetime import datetime

from .errors import PodgroveError

DEFAULT_INIT_TIMEOUT = 300


class StorageInitTimeout(PodgroveError):
    """The last observed storage initializer exhausted its own deadline."""


class StorageInitWatch:
    def __init__(self, namespace: str, timeout: float = DEFAULT_INIT_TIMEOUT):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise PodgroveError("Storage initialization timeout must be finite and positive")
        self.namespace, self.timeout = namespace, timeout
        self.starts = {}
        self.contexts = {}
        self.deadline = None
        self.context = ""

    def observe(self, pod: dict | None) -> None:
        if not pod:
            self.deadline = None
            return
        statuses = pod.get("status", {}).get("initContainerStatuses", [])
        storage = next((item for item in statuses if item.get("name") == "storage"), None)
        uid = pod["metadata"]["uid"]
        if storage is None:
            if uid in self.starts:
                self.deadline = self.starts[uid] + self.timeout
                self.context = self.contexts[uid]
            else:
                self.deadline = None
            self.check()
            return
        state = storage.get("state", {})
        if state.get("terminated", {}).get("exitCode") == 0:
            self.deadline = None
            self.starts.pop(uid, None)
            self.contexts.pop(uid, None)
            return
        run = state.get("running") or state.get("terminated") or storage.get("lastState", {}).get("terminated", {})
        if "running" not in state and not run and uid not in self.starts:
            self.deadline = None
            return
        now = time.monotonic()
        started = now
        stamp = run.get("startedAt")
        if isinstance(stamp, str):
            try:
                timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if timestamp.tzinfo is not None:
                    started -= max(0, time.time() - timestamp.timestamp())
            except (ValueError, OverflowError, OSError):
                pass
        self.starts[uid] = min(self.starts.get(uid, now), started)
        self.deadline = self.starts[uid] + self.timeout
        metadata, spec = pod["metadata"], pod.get("spec", {})
        claims = [item["persistentVolumeClaim"].get("claimName", "unknown")
                  for item in spec.get("volumes", []) if "persistentVolumeClaim" in item]
        self.context = (f"Pod {self.namespace}/{metadata['name']}; node {spec.get('nodeName', 'unassigned')}; "
                        f"PVC {', '.join(self.namespace + '/' + name for name in claims) or 'unknown'}; "
                        f"storage initializer {next(iter(state), 'unknown')}")
        self.contexts[uid] = self.context
        self.check()

    def check(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise StorageInitTimeout(
                f"Storage initialization timed out after {self.timeout:g}s: {self.context}. "
                "Completion was not observed before the deadline; inspect this Pod, node and PVC. "
                "The cause is not established by this timeout. Resources retained for diagnosis; "
                "use podgrove down for scoped cleanup or --init-timeout to allow a longer initialization."
            )

    def limit(self, deadline: float) -> float:
        self.check()
        return min(deadline, self.deadline) if self.deadline is not None else deadline
