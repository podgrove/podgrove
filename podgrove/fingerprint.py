"""Stable launch fingerprints with narrowly bounded legacy compatibility.

Only digests are persisted: normalized Compose models can contain secrets.
The pre-network format hashed model, forward and ttl. Adding default network
settings changed the hash without changing the deployed application.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .config import Config


FORMAT = "compose-v1"


def _digest(payload: dict) -> str:
    # Preserve the existing serialization, including its whitespace. Sorting
    # mapping keys is safe; sorting sequences could change Compose semantics.
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class LaunchFingerprint:
    digest: str
    legacy_digest: str | None

    def matches(self, previous: str, recorded_format: str | None = None) -> bool:
        if recorded_format not in (None, FORMAT):
            return False
        if previous == self.digest:
            return True
        return recorded_format is None and self.legacy_digest is not None and previous == self.legacy_digest


def launch_fingerprint(model: dict, config: Config) -> LaunchFingerprint:
    original = {"model": model, "forward": config.forward, "ttl": config.ttl_seconds}
    current = {**original, "network": config.network}
    if config.sync_exclude:
        current["sync_exclude"] = config.sync_exclude
    for key in ("placement", "reverse", "connect"):
        if value := getattr(config, key):
            current[key] = value
    legacy = (_digest(original) if config.network == {"blocked_cidrs": []} and not config.sync_exclude
              and not any(getattr(config, key) for key in ("placement", "reverse", "connect"))
              else None)
    return LaunchFingerprint(_digest(current), legacy)
