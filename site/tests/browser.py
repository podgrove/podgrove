#!/usr/bin/env python3
"""Exercise the production Pages build in an isolated browser and local server."""
from __future__ import annotations

import argparse
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import unquote, urlsplit

from check_build import BASE, SITE, check_build


class PagesHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if not path.startswith(BASE):
            self.send_error(404, "Outside the Pages base")
            return
        relative = path.removeprefix(BASE)
        target = (Path(self.directory) / relative).resolve()
        if not target.is_relative_to(Path(self.directory).resolve()) or target.is_dir() and not (target / "index.html").is_file():
            self.send_error(404, "No directory listing")
            return
        self.path = "/" + relative + ("?" + parsed.query if parsed.query else "")
        super().do_GET()

    def log_message(self, *_args):
        pass


def assert_fit(page, label):
    measurements = page.evaluate("""() => ({
        viewport: innerWidth,
        html: document.documentElement.scrollWidth,
        body: document.body.scrollWidth,
        offenders: [...document.querySelectorAll('main *, header *')].filter(e => {
            const r = e.getBoundingClientRect();
            return r.width && (r.right > innerWidth + 1 || r.left < -1)
                && !e.closest('pre, [aria-hidden="true"], .expressive-code');
        }).slice(0, 6).map(e => ({tag: e.tagName, class: String(e.className)}))
    })""")
    assert max(measurements["html"], measurements["body"]) <= measurements["viewport"] + 1, (label, measurements)
    return {"label": label, **measurements}


def select_theme(page, theme):
    selector = page.locator("starlight-theme-select select:visible").first
    if not selector.count():
        page.get_by_role("button", name="Menu", exact=True).click()
        selector = page.locator("starlight-theme-select select:visible").first
    selector.select_option(theme)
    page.wait_for_function("theme => document.documentElement.dataset.theme === theme", arg=theme)
    menu = page.locator("#starlight__sidebar:popover-open")
    if menu.count():
        page.get_by_role("button", name="Menu", exact=True).click()


def assert_overview(page, expect, theme):
    content = page.locator("main .sl-markdown-content")
    words = content.inner_text().split()
    assert len(words) <= 450, ("Overview should stay concise; details belong in architecture", len(words))
    expect(page.locator('main [class*="language-mermaid"]:visible')).to_have_count(0)
    assert not re.search(r"\b(?:flowchart|graph)\s+(?:LR|TD|TB|RL|BT)\b", content.inner_text(), re.I), "Raw chart syntax is visible"
    expect(content.locator(f'a[href="{BASE}architecture/"]')).to_be_visible()
    diagram = content.locator(f'img[src="{BASE}diagrams/architecture.svg"]')
    expect(diagram).to_have_count(1)
    expect(diagram).to_be_visible()
    expect(diagram).to_have_js_property("complete", True)
    image = diagram.evaluate("""e => ({
        src: e.currentSrc, alt: e.alt, naturalWidth: e.naturalWidth, naturalHeight: e.naturalHeight,
        width: e.getBoundingClientRect().width, height: e.getBoundingClientRect().height,
        colorScheme: getComputedStyle(e).colorScheme
    })""")
    assert image["naturalWidth"] > 0 and image["naturalHeight"] > 0, ("Diagram did not decode", image)
    assert image["width"] > 0 and image["height"] > 0 and len(image["alt"].strip()) >= 16, image
    assert image["colorScheme"] == theme, ("Diagram does not inherit the manually selected theme", image)
    bounds = content.evaluate("""root => ({
        viewport: innerWidth,
        offenders: [root, ...root.querySelectorAll('*')].filter(e => {
            const r = e.getBoundingClientRect();
            const c = getComputedStyle(e);
            return r.width > 0 && r.height > 0 && c.visibility !== 'hidden'
                && (r.left < -1 || r.right > innerWidth + 1);
        }).slice(0, 10).map(e => ({tag: e.tagName, class: String(e.className),
            left: e.getBoundingClientRect().left, right: e.getBoundingClientRect().right}))
    })""")
    assert not bounds["offenders"], ("Overview content extends outside viewport", bounds)
    return {"overview": "loaded SVG, concise content, linked detail, no raw chart or overflow",
            "word_count": len(words), "image": image, **bounds}


def run(dist: Path, output: Path):
    from playwright.sync_api import expect, sync_playwright

    build = check_build(dist)
    assert build["status"] == "passed", build["errors"]
    report = {"status": "running", "build": build, "checks": [], "screenshots": [], "errors": [], "requests": []}
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(PagesHandler, directory=str(dist)))
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    report["origin"] = origin
    report["network_scope"] = "Only this owned loopback server; external HTTP requests are blocked"
    try:
        with sync_playwright() as playwright:
            configured = os.environ.get("PODGROVE_BROWSER_EXECUTABLE")
            chrome = Path(configured) if configured else Path(playwright.chromium.executable_path)
            if not chrome.is_file() and not configured:
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            assert chrome.is_file(), "Install Playwright Chromium or set PODGROVE_BROWSER_EXECUTABLE"
            browser = playwright.chromium.launch(executable_path=str(chrome), headless=True)
            report["browser"] = {"version": browser.version, "executable": str(chrome)}
            try:
                for width in (1440, 390, 320):
                    context = browser.new_context(viewport={"width": width, "height": 1000 if width > 400 else 844},
                                                  reduced_motion="reduce", color_scheme="light")
                    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/")
                                  else route.abort("blockedbyclient"))
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    page.on("pageerror", lambda error: report["errors"].append({"kind": "page", "detail": str(error)}))
                    page.on("console", lambda event: report["errors"].append({"kind": "console", "detail": event.text})
                            if event.type == "error" else None)
                    page.on("requestfailed", lambda request: report["errors"].append({"kind": "request", "url": request.url,
                                                                                    "detail": request.failure}))
                    page.on("response", lambda response: report["requests"].append({"url": response.url, "status": response.status}))
                    try:
                        for theme in ("light", "dark"):
                            page.goto(origin + BASE, wait_until="networkidle")
                            select_theme(page, theme)
                            page.reload(wait_until="networkidle")
                            expect(page.locator("html")).to_have_attribute("data-theme", theme)
                            report["checks"].append({"theme_persistence": theme, "width": width})
                            for route in ("", "getting-started/", "configuration/", "how-it-works/"):
                                page.goto(origin + BASE + route, wait_until="networkidle")
                                expect(page.locator("main h1")).to_be_visible()
                                expect(page.locator("html")).to_have_attribute("data-theme", theme)
                                report["checks"].append(assert_fit(page, f"{width}-{theme}-{route or 'home'}"))
                                if route == "how-it-works/":
                                    report["checks"].append({"width": width, "theme": theme, **assert_overview(page, expect, theme)})
                                name = f"{width}-{theme}-{route.strip('/') or 'home'}.png"
                                page.screenshot(path=str(output / name), full_page=True)
                                report["screenshots"].append(name)
                            page.goto(origin + BASE, wait_until="networkidle")
                            start = page.locator(f'a[href="{BASE}getting-started/"]:visible').first
                            start.click()
                            expect(page).to_have_url(origin + BASE + "getting-started/")
                            configuration = page.locator(f'a[href="{BASE}configuration/"]:visible').first
                            if not configuration.count():
                                page.get_by_role("button", name="Menu", exact=True).click()
                                configuration = page.locator(f'a[href="{BASE}configuration/"]:visible').first
                            configuration.click()
                            expect(page).to_have_url(origin + BASE + "configuration/")
                            report["checks"].append({"navigation": "home → getting started → configuration", "width": width, "theme": theme})
                            search = page.get_by_role("button", name="Search", exact=True)
                            expect(search).to_be_enabled()
                            search.focus()
                            page.keyboard.press("Enter")
                            search_input = page.get_by_role("dialog", name="Search", exact=True).get_by_role("textbox", name="Search", exact=True)
                            expect(search_input).to_be_visible()
                            search_input.fill("reverse")
                            result = page.locator("dialog[open] .pagefind-ui__result-link").first
                            expect(result).to_be_visible(timeout=20000)
                            href = result.get_attribute("href")
                            assert href and (href.startswith(BASE) or href.startswith(origin + BASE)), href
                            expect(search_input).to_be_focused()
                            focus = search_input.evaluate("""e => { const c = getComputedStyle(e); return {
                                focusVisible: e.matches(':focus-visible'), outline: c.outlineStyle,
                                outlineWidth: c.outlineWidth, outlineOffset: c.outlineOffset,
                                outlineColor: c.outlineColor, boxShadow: c.boxShadow, border: c.borderColor
                            }; }""")
                            assert focus["focusVisible"], focus
                            assert focus["boxShadow"] == "none", ("Extra shadow around focused search", focus)
                            if focus["outline"] != "none" and float(focus["outlineWidth"].removesuffix("px")) > 0:
                                assert float(focus["outlineOffset"].removesuffix("px")) <= 0, focus
                                assert focus["border"] == focus["outlineColor"], ("Separate focus border and outline", focus)
                            report["checks"].append({"search": "reverse", "result": href, "width": width,
                                                     "theme": theme, "focus": focus})
                            report["checks"].append(assert_fit(page, f"{width}-{theme}-search"))
                            name = f"{width}-{theme}-search.png"
                            page.screenshot(path=str(output / name), full_page=True)
                            report["screenshots"].append(name)
                            result.click()
                            page.wait_for_load_state("networkidle")
                            assert page.url.startswith(origin + BASE), page.url
                            assert page.locator("main h1").is_visible()
                    finally:
                        context.close()
                bad = [request for request in report["requests"] if request["status"] >= 400]
                assert not bad, bad
                assert not report["errors"], report["errors"]
                assert any("/pagefind/" in request["url"] for request in report["requests"]), "Search did not load its production index"
                assert check_build(dist)["files"] == build["files"], "Built files changed during browser checks"
                report["status"] = "passed"
            finally:
                browser.close()
    except BaseException as error:
        report["status"] = "failed"
        report["failure"] = {"type": type(error).__name__, "detail": str(error)[:8192]}
        raise
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=5)
        report["screenshot_sha256"] = {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                                        for name in report["screenshots"]}
        (output / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=SITE / "dist")
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory outside source, or under artifacts/")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    root = SITE.parent.resolve()
    if output.is_relative_to(root) and not output.is_relative_to(root / "artifacts"):
        parser.error("Evidence must be outside the source checkout or under its ignored artifacts/ directory")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        report = run(args.dist.resolve(strict=True), output)
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({"status": "failed", "output": str(output), "type": type(error).__name__,
                          "detail": str(error).split("Aria snapshot:", 1)[0][:2000]}))
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]),
                      "screenshots": len(report["screenshots"]), "output": str(output)}))


if __name__ == "__main__":
    raise SystemExit(main())
