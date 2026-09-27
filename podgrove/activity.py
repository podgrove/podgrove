"""Track edits for idle TTL even when Compose Watch owns all file delivery."""
from __future__ import annotations

import os
from pathlib import Path


class WatchActivity:
    def __init__(self, model: dict):
        self.paths = [Path(rule["path"]) for service in model.get("services", {}).values()
                      for rule in service.get("develop", {}).get("watch", [])]
        self.previous = self.snapshot()

    def snapshot(self) -> dict:
        result = {}
        for path in self.paths:
            paths = [path]
            if path.is_dir():
                for directory, dirs, files in os.walk(path, followlinks=False):
                    dirs[:] = [d for d in dirs if d != ".git" and not (Path(directory) / d).is_symlink()]
                    paths.extend(Path(directory) / f for f in files if f != ".git")
            for file in paths:
                try:
                    stat = file.lstat()
                    result[str(file)] = (stat.st_mtime_ns, stat.st_size, stat.st_mode)
                except FileNotFoundError:
                    pass
        return result

    def changed(self) -> bool:
        current = self.snapshot()
        changed = current != self.previous
        self.previous = current
        return changed
