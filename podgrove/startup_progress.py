"""Small, public startup observations without retaining Compose configuration."""
from __future__ import annotations

import codecs
import re
import time
from pathlib import Path


class StartupProgress:
    def __init__(self, publish, started_at=None):
        self.publish = publish
        self.started_at = started_at if isinstance(started_at, (int, float)) else time.time()
        self.phase = "preparing environment"
        self.services = {}
        self.vertices = {}
        self.containers = {}
        self.builders = []
        self.images = {}
        self.building_images = set()

    def snapshot(self):
        return {"phase": self.phase, "started_at": self.started_at, "updated_at": time.time(),
                "elapsed_seconds": round(max(0, time.time() - self.started_at), 1),
                "services": [dict(service) for service in self.services.values()]}

    def configure(self, model):
        self.services = {name: {"service": name, "state": "pending"}
                         for name, service in model["services"].items()
                         if service.get("deploy", {}).get("replicas", service.get("scale", 1)) != 0}
        project = model.get("name", "")
        self.containers = {service.get("container_name", f"{project}-{name}"): name
                           for name, service in model["services"].items() if name in self.services}
        self.builders = [name for name, service in model["services"].items() if service.get("build") and name in self.services]
        self.images = {}
        self.building_images.clear()
        for name, service in model["services"].items():
            if name in self.services:
                image = service.get("image", f"{project}-{name}")
                self.images.setdefault(image, []).append(name)
        self.publish(self.snapshot())

    def set_phase(self, phase):
        self.phase = phase
        self.publish(self.snapshot())

    def output(self, line):
        image = re.match(r"\s*Image\s+(\S+)\s+(Building|Built|Pulling|Pulled|Error)\b", line)
        if image:
            if image[2] == "Building":
                self.building_images.add(image[1])
            elif image[2] in ("Built", "Error"):
                self.building_images.discard(image[1])
            for name in self.images.get(image[1], []):
                self.services[name].update(state=image[2].lower(), progress=image[2].lower())
            self.publish(self.snapshot())
            return
        step = re.match(r"\s*Step\s+(\d+)/(\d+)\s*:", line)
        if step:
            active = {name for image in self.building_images for name in self.images.get(image, [])}
            candidates = active if self.building_images else set(self.builders)
            if any(image not in self.images for image in self.building_images):
                candidates = set()
            if len(candidates) == 1:
                name = next(iter(candidates))
                self.services[name].update(state="building", progress=f"step {step[1]}/{step[2]}")
                self.publish(self.snapshot())
            return
        vertex = re.match(r"#(\d+) \[([^\s\]]+)([^\]]*)\]", line)
        name = (vertex[2] if vertex and vertex[2] in self.services else
                self.builders[0] if vertex and len(self.builders) == 1 else None)
        if name is not None:
            self.vertices[vertex[1]] = name
            details = (vertex[3] if vertex[2] == name else vertex[2] + vertex[3]).strip()
            self.services[name].update(state="building", progress=details or "preparing build")
        else:
            completion = re.match(r"#(\d+) (DONE|CACHED|ERROR)(.*)", line)
            if completion and completion[1] in self.vertices:
                name = self.vertices[completion[1]]
                self.services[name]["progress"] = completion[2].lower() + completion[3]
                if completion[2] == "ERROR":
                    self.services[name]["state"] = "build failed"
            else:
                container = re.match(r"\s*Container\s+(\S+)\s+(\S+)", line)
                if container:
                    matches = [name for prefix, name in self.containers.items()
                               if container[1] == prefix or re.fullmatch(re.escape(prefix) + r"-\d+", container[1])]
                    if len(matches) == 1:
                        self.services[matches[0]].update(state=container[2].lower())
                        self.services[matches[0]].pop("progress", None)
                        self.publish(self.snapshot())
                    return
                match = re.match(r"\s*(?:Service\s+)?(\S+)\s+(Building|Built|Pulling|Pulled|Error)\b", line)
                if not match or match[1] not in self.services:
                    return
                self.services[match[1]].update(state=match[2].lower(), progress=match[2].lower())
        self.publish(self.snapshot())

    def observe(self, rows):
        for name, service in self.services.items():
            containers = [row for row in rows if row.get("Service") == name]
            if containers:
                service["state"] = ", ".join(sorted({str(row.get("State", "unknown"))
                    + ("/" + row["Health"] if row.get("Health") else "") for row in containers}))
                service.pop("progress", None)
        self.publish(self.snapshot())


class StartupLog:
    """Follow only the current launch's log, keeping machine-readable stdout clean."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.offset = path.stat().st_size
        except FileNotFoundError:
            self.offset = 0
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.has_more = False

    def read(self, *, final=False):
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read(256 * 1024)
                self.offset = stream.tell()
        except FileNotFoundError:
            return ""
        self.has_more = len(chunk) == 256 * 1024
        text = self.pending + self.decoder.decode(chunk, final=final and not self.has_more)
        boundary = text.rfind("\n") + 1
        if (final and not self.has_more) or len(text) > 256 * 1024:
            boundary = len(text)
        self.pending = text[boundary:]
        return text[:boundary]
