#!/usr/bin/env python3
"""Validate the built Pages site without starting a server or making requests."""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
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
        self.in_style = False
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
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
        for kind, reference in document.references:
            check_reference(path, kind, reference)
        for style in document.styles:
            for _, reference in CSS_URL.findall(style):
                check_reference(path, "inline CSS url", reference)
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
