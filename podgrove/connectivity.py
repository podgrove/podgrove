"""Session lifecycle for declared links and local reverse TCP forwarding."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time

from .connect import overlay_model, validate_connectivity
from .errors import PodgroveError


class Connectivity:
    def __init__(self, kube, ident, compose, model, expected_uids, *, reconcile_connections=False):
        self.kube, self.ident, self.compose, self.model = kube, ident, compose, model
        self.expected_uids = expected_uids
        self.reconcile_connections = reconcile_connections
        self.links = self.reverse = self.overlay = None

    def start(self, *, deadline=None, cancel_event=None):
        done = threading.Event()
        def active():
            if (cancel_event is not None and cancel_event.is_set()
                    or deadline is not None and time.monotonic() >= deadline):
                raise PodgroveError("Connectivity startup cancelled or its deadline expired")
        def cancel_start():
            while not done.wait(.02):
                try:
                    active()
                except PodgroveError:
                    for component in (self.links, self.reverse):
                        if component is not None:
                            component.cancel()
        watcher = None
        try:
            active()
            if deadline is not None or cancel_event is not None:
                watcher = threading.Thread(target=cancel_start, name="podgrove-connect-start", daemon=True)
                watcher.start()
            self._start(active)
            active()
            return self
        except BaseException:
            self.close()
            raise
        finally:
            done.set()
            if watcher is not None:
                watcher.join(timeout=1)

    def _start(self, active):
        from .reverse import ReverseForward
        config = self.compose.config
        validate_connectivity(config, self.model, self.ident)
        aliases = {}
        if config.connect or self.reconcile_connections:
            raise PodgroveError("Legacy connect grants are refused; remove them with the previous version's scoped down, then configure network.pod_to_pod")
        if config.reverse:
            self.reverse = ReverseForward(self.kube, self.ident, config.reverse, self.expected_uids)
            active()
            self.reverse.start()
            aliases["host.docker.internal"] = "host-gateway"
        if aliases:
            active()
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="podgrove-connect-", delete=False) as stream:
                self.overlay = Path(stream.name)
                json.dump(overlay_model(self.model, aliases), stream)
            self.compose.overlay_files.append(self.overlay)
        return self

    def snapshot(self):
        links = self.links.snapshot() if self.links else {"state": "disabled"}
        reverse = self.reverse.snapshot() if self.reverse else {"state": "disabled"}
        states = {item["state"] for item in (links, reverse)}
        status = "disabled" if states == {"disabled"} else "ready" if states <= {"disabled", "ready"} else "disconnected"
        return {"state": status, "connect": links, "reverse": reverse, "checked_at": time.time()}

    def check(self):
        return self.snapshot()

    def close(self):
        errors = []
        for component in (self.reverse, self.links):
            if component is not None:
                try:
                    component.close()
                except Exception as exc:
                    errors.append(exc)
        if self.overlay is not None:
            if self.overlay in self.compose.overlay_files:
                self.compose.overlay_files.remove(self.overlay)
            self.overlay.unlink(missing_ok=True)
            self.overlay = None
        if errors:
            raise errors[0]
