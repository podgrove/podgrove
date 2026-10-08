#!/usr/bin/env python3
"""Validate the built Pages site without starting a server or making requests."""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import struct
from urllib.parse import unquote, urljoin, urlsplit

SITE = Path(__file__).resolve().parents[1]
BASE = "/podgrove/"
ORIGIN = "https://podgrove.github.io"
REPOSITORY = "https://github.com/podgrove/podgrove/blob/main/"
CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)
CSS_IMPORT = re.compile(r"@import\s+(['\"])(.*?)\1", re.I)


class Document(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.references: list[tuple[str, str]] = []
        self.styles: list[str] = []
        self.metadata: dict[str, list[str]] = {}
        self.icons: list[dict[str, str]] = []
        self.in_style = False
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and (key := values.get("property") or values.get("name")):
            self.metadata.setdefault(key, []).append(values.get("content", ""))
        if tag == "link" and values.get("rel") in ("icon", "apple-touch-icon"):
            self.icons.append(values)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "a" and values.get("name"):
            self.ids.add(values["name"])
        for name in ("href", "src", "poster", "data"):
            value = values.get(name)
            if value and (name != "data" or tag == "object"):
                self.references.append((f"{tag}[{name}]", value))
        if values.get("srcset") and not values["srcset"].lstrip().startswith("data:"):
            for candidate in values["srcset"].split(","):
                if candidate.strip():
                    self.references.append((f"{tag}[srcset]", candidate.strip().split()[0]))
        if values.get("style"):
            self.styles.append(values["style"])
        if tag == "style":
            self.in_style = True

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            self.styles.append(data)


def page_mapping() -> dict[str, str]:
    text = (SITE / "scripts/pages.mjs").read_text()
    pairs = re.findall(r"source:\s*'([^']+)'\s*,\s*slug:\s*'([^']+)'", text)
    if not pairs or len({source for source, _ in pairs}) != len(pairs) or len({slug for _, slug in pairs}) != len(pairs):
        raise ValueError("Expected nonempty, unique documentation sources and routes in pages.mjs")
    for source, slug in pairs:
        if (source == "README.md" or any(part in ("", ".", "..") for part in source.split("/"))
                or not re.fullmatch(r"[A-Za-z0-9_./-]+\.md", source)
                or any(part in ("", ".", "..") for part in slug.split("/"))
                or not re.fullmatch(r"[a-z0-9/-]+", slug)):
            raise ValueError("Documentation sources and slugs must be safe relative paths; README.md is the home route")
    return {source: BASE + slug + "/" for source, slug in pairs} | {"README.md": BASE}


def document_url(path: Path, dist: Path) -> str:
    relative = path.relative_to(dist).as_posix()
    if relative == "index.html":
        relative = ""
    elif relative.endswith("/index.html"):
        relative = relative[:-len("index.html")]
    return ORIGIN + BASE + relative


def check_build(dist: Path) -> dict:
    dist = dist.resolve(strict=True)
    mapping = page_mapping()
    errors: list[str] = []
    documents = {}
    hashes = {}
    for path in sorted(dist.rglob("*")):
        if path.is_symlink():
            errors.append(f"Symlink in built output: {path.relative_to(dist)}")
        elif path.is_file():
            hashes[path.relative_to(dist).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            if path.suffix in (".html", ".svg"):
                documents[path] = Document(path.read_text())
    for route in mapping.values():
        target = dist / route.removeprefix(BASE) / "index.html"
        if target not in documents:
            errors.append(f"Missing required route: {route}")
    if not (dist / "pagefind/pagefind.js").is_file():
        errors.append("Missing production Pagefind search index loader")
    checked = anchors = 0

    def check_reference(path, kind, reference):
        nonlocal checked, anchors
        if reference.startswith(REPOSITORY):
            source = unquote(urlsplit(reference.removeprefix(REPOSITORY)).path)
            if source in mapping:
                errors.append(f"{path.relative_to(dist)}: {kind} should use {mapping[source]}: {reference}")
            return
        parsed = urlsplit(urljoin(document_url(path, dist), reference))
        if parsed.scheme not in ("http", "https") or parsed.netloc != urlsplit(ORIGIN).netloc:
            return
        checked += 1
        url_path = unquote(parsed.path)
        if not url_path.startswith(BASE):
            errors.append(f"{path.relative_to(dist)}: {kind} leaves Pages base: {reference}")
            return
        target = (dist / url_path.removeprefix(BASE)).resolve()
        if not target.is_relative_to(dist):
            errors.append(f"{path.relative_to(dist)}: {kind} escapes build: {reference}")
            return
        if target.is_dir():
            target /= "index.html"
        if not target.is_file():
            errors.append(f"{path.relative_to(dist)}: missing {kind} target: {reference}")
            return
        fragment = unquote(parsed.fragment).split(":~:text=", 1)[0]
        if fragment and target.suffix in (".html", ".svg"):
            anchors += 1
            if target not in documents or fragment not in documents[target].ids:
                errors.append(f"{path.relative_to(dist)}: missing anchor: {reference}")

    for path, document in documents.items():
        if path.suffix == ".html":
            preview = ORIGIN + BASE + "brand/share.png"
            required = {"og:image": preview, "twitter:image": preview, "og:image:type": "image/png",
                        "og:image:width": "1200", "og:image:height": "630", "twitter:card": "summary_large_image"}
            for key, expected in required.items():
                if document.metadata.get(key) != [expected]:
                    errors.append(f"{path.relative_to(dist)}: expected one {key} with value {expected}")
            for key in ("og:image:alt", "twitter:image:alt"):
                values = document.metadata.get(key, [])
                if len(values) != 1 or not values[0].strip():
                    errors.append(f"{path.relative_to(dist)}: missing or ambiguous {key}")
            for rel, size in (("icon", 48), ("apple-touch-icon", 180)):
                expected = ORIGIN + BASE + f"brand/icon-{size}.png"
                if not any(icon.get("rel") == rel and icon.get("href") == expected for icon in document.icons):
                    errors.append(f"{path.relative_to(dist)}: missing {rel} PNG: {expected}")
            for key in ("og:image", "twitter:image"):
                for reference in document.metadata.get(key, []):
                    check_reference(path, key, reference)
        for kind, reference in document.references:
            check_reference(path, kind, reference)
        for style in document.styles:
            for _, reference in CSS_URL.findall(style):
                check_reference(path, "inline CSS url", reference)
    for name, dimensions in (("share", (1200, 630)), ("icon-48", (48, 48)), ("icon-180", (180, 180))):
        path = dist / "brand" / f"{name}.png"
        content = path.read_bytes() if path.is_file() else b""
        if (len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR"
                or struct.unpack(">II", content[16:24]) != dimensions):
            errors.append(f"brand/{name}.png: expected a PNG with dimensions {dimensions[0]}x{dimensions[1]}")
    for path in sorted(dist.rglob("*.css")):
        text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
        for _, reference in CSS_URL.findall(text) + CSS_IMPORT.findall(text):
            check_reference(path, "CSS asset", reference)
    return {"status": "failed" if errors else "passed", "base": BASE, "dist": str(dist),
            "required_pages": len(mapping), "html_pages": sum(path.suffix == ".html" for path in documents),
            "internal_references": checked, "fragment_references": anchors,
            "files": hashes, "errors": sorted(set(errors))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=SITE / "dist")
    parser.add_argument("--output", type=Path, help="Optional new JSON evidence file")
    args = parser.parse_args(argv)
    result = check_build(args.dist)
    if args.output:
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2, sort_keys=True)
            stream.write("\n")
    print(json.dumps({key: value for key, value in result.items() if key != "files"}, indent=2))
    return result["status"] != "passed"


if __name__ == "__main__":
    raise SystemExit(main())
