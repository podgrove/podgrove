"""Real-browser dashboard checks against an inert local HTTP fixture provider.

Install the optional web-test dependencies to run. Chrome is launched with a
fresh temporary profile; no developer profile, Kubernetes or Docker is used.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import os
import queue
import re
import threading

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
pytestmark = pytest.mark.browser
SCREENSHOTS = Path(__file__).resolve().parents[1] / "artifacts" / "ui"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
IDENT = "012345abcdef"
SECOND = "fedcba543210"
UNSAFE_LOG = '<img src=x onerror="window.__log_xss=true"><script>window.__log_xss=true</script>'


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as playwright:
        configured = os.environ.get("PODGROVE_BROWSER_EXECUTABLE")
        executable = Path(configured) if configured else CHROME
        if not executable.is_file():
            executable = Path(playwright.chromium.executable_path)
        if not executable.is_file():
            pytest.skip("Install Chrome/Playwright Chromium or set PODGROVE_BROWSER_EXECUTABLE")
        instance = playwright.chromium.launch(executable_path=str(executable), headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce",
                                  color_scheme="light")
    current = context.new_page()
    current.set_default_timeout(5000)
    errors = []
    current.on("pageerror", lambda error: errors.append(str(error)))
    yield current
    context.close()
    assert not errors, f"Browser JavaScript errors: {errors}"


def snapshot(ident=IDENT, name="checkout-api", *, connected=True):
    ports = [{"service": "api", "target": 8000, "local": 49152, "url": "http://127.0.0.1:49152"}]
    services = [{"name": "api", "state": "running", "health": "healthy", "image": "example/api:development",
                 "replicas": 1, "containers": [{"id": "a" * 64, "name": "checkout-api-1", "state": "running",
                                                "health": "healthy", "image": "example/api:development"}], "ports": ports},
                {"name": "database", "state": "running", "health": "unhealthy", "image": "postgres:17",
                 "replicas": 1, "containers": [{"id": "b" * 64, "name": "checkout-database-1", "state": "running",
                                                "health": "unhealthy", "image": "postgres:17"}], "ports": []}]
    return {"identity": ident, "name": name, "root": f"/Users/developer/projects/checkout/{name}",
            "worktree": name, "branch": "feature/review-dashboard", "repository": "checkout",
            "context": "fixture:development", "namespace_mode": "shared",
            "namespace": "podgrove-testing", "status": "ready" if connected else "disconnected",
            "node_mode": "shared", "created_at": 1750000000, "last_activity": 1750000300,
            "ttl_seconds": 14400, "ports": ports, "services": services,
            "counts": {"total": 2, "running": 2, "healthy": 1, "unhealthy": 1, "exited": 0},
            "source": "live" if connected else "local_snapshot", "health_fresh": connected,
            "health_observed_at": 1750000400 if connected else None, "observed_at": 1750000400,
            "engine": {"statefulset": {"name": f"pg-{ident}", "uid": "controller-fixture", "replicas": 1, "ready_replicas": 1},
                       "pod": {"name": f"pg-{ident}-0", "uid": "pod-fixture", "phase": "Running", "ready": True,
                               "restarts": 0, "node": "development-node-a", "resources": {
                                   "requests": {"cpu": "250m", "memory": "2Gi"}, "limits": {"cpu": "2", "memory": "2Gi"}}}}
                      if connected else None,
            "storage": {"name": f"pg-{ident}", "uid": "claim-fixture", "phase": "Bound", "requested": "20Gi",
                        "capacity": "20Gi", "storage_class": "gp3", "volume": "pvc-fixture-volume"}
                       if connected else None,
            "configuration": {"source": "current_file", "status": "available", "file": "podgrove.yml", "warning": None,
                              "settings": {"version": 1, "size": "small", "node_mode": "shared", "ttl_seconds": 28800,
                                           "resources_mode": "preset", "resources": {
                                               "requests": {"cpu": "250m", "memory": "2Gi"},
                                               "limits": {"cpu": "2", "memory": "2Gi"}},
                                           "init_resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                                              "limits": {"cpu": "100m", "memory": "32Mi"}},
                                           "storage": {"size": "20Gi"},
                                           "cluster": {"context": "configured:next-cluster", "namespace": "wt-next"},
                                           "compose": {"files": ["compose.yml", "compose.dev.yml"], "profiles": ["debug"],
                                                       "project_directory": "."},
                                           "forward": [{"service": "api", "port": 8000}]}},
            "warnings": [] if connected else ["Environment session is disconnected; showing the local snapshot"]}


class FixtureDashboard:
    def __init__(self):
        self.snapshots = {IDENT: snapshot(), SECOND: snapshot(SECOND, "payments-worker", connected=False)}
        self.calls = []
        self.fail_inventory = self.fail_detail = self.fail_logs = False
        self.fail_settings = False
        self.fail_live = False
        self.live_streams = []
        self.live_records = [
            {"type": "line", "text": "live fixture ready 🌿\n", "container": None, "stream": "stdout"},
            {"type": "line", "text": UNSAFE_LOG + "\n", "container": None, "stream": "stderr"},
        ]
        self.log_text = "2026-09-25T12:00:00Z api ready\n" + UNSAFE_LOG + "\n" + "long-line-" * 70

    def environments(self):
        from podgrove.web import WebError
        self.calls.append(("environments",))
        if self.fail_inventory:
            raise WebError("Fixture inventory temporarily unavailable")
        rows = [{key: value for key, value in data.items() if key not in ("engine", "storage", "warnings")}
                for data in self.snapshots.values()]
        return deepcopy({"context": "fixture:development", "environments": rows, "errors": [], "observed_at": 1750000400})

    def detail(self, ident):
        from podgrove.web import WebError
        self.calls.append(("detail", ident))
        if self.fail_detail:
            raise WebError("Fixture detail temporarily unavailable")
        if ident not in self.snapshots:
            raise WebError("Unknown environment", 404)
        return deepcopy(self.snapshots[ident])

    def logs(self, ident, *, source, service, tail, container=None):
        from podgrove.web import WebError
        self.calls.append(("logs", ident, source, service, tail))
        if container:
            self.calls.append(("container_logs", ident, service, container))
        assert 1 <= tail <= 200
        if self.fail_logs:
            raise WebError("Fixture logs temporarily unavailable")
        return {"source": source, "service": service, "tail": tail, "text": self.log_text,
                "truncated": False, "observed_at": 1750000400}

    def logs_stream(self, ident, *, source, service, tail, container=None):
        from podgrove.web import WebError
        self.calls.append(("logs_stream", ident, source, service, tail, container))
        if self.fail_live:
            raise WebError("Fixture live logs are unavailable", 429)
        selected = next((item for item in self.snapshots[ident]["services"] if item["name"] == service), None)
        if selected and len(selected["containers"]) > 8 and container is None:
            raise WebError("Select one container when a service has more than eight replicas", 400)
        stream = FixtureLogStream(self.live_records, source, service, tail)
        self.live_streams.append(stream)
        return stream

    def settings(self, namespace=None):
        from podgrove.web import WebError
        self.calls.append(("settings", namespace))
        if self.fail_settings:
            raise WebError("Fixture configuration temporarily unavailable")
        options = sorted({row["namespace"] for row in self.snapshots.values()})
        selected = namespace or (options[0] if options else None)
        return {"context": "fixture:development", "read_only": True, "namespace_options": options,
                "selected_namespace": selected,
                "namespace": None,
                "provisioning": {"kind": "ConfigMap", "name": "podgrove-bootstrap", "status": "present",
                                 "version": "1", "namespace_mode": "shared", "environment": None}
                if selected else None,
                "access": {"objects": [
                    {"kind": "ServiceAccount", "name": "podgrove-client", "namespace": selected,
                     "status": "present", "uid": "sa-fixture", "automount_service_account_token": False},
                    {"kind": "Role", "name": "podgrove-client", "namespace": selected,
                     "status": "present", "uid": "role-fixture", "rules": [
                         {"api_groups": [""], "resources": ["pods", "pods/log"], "verbs": ["get", "list"],
                          "resource_names": [], "non_resource_urls": []}]},
                    {"kind": "RoleBinding", "name": "podgrove-client", "namespace": selected,
                     "status": "present", "uid": "binding-fixture",
                     "role_ref": {"api_group": "rbac.authorization.k8s.io", "kind": "Role", "name": "podgrove-client"},
                     "subjects": [{"kind": "ServiceAccount", "name": "podgrove-client", "namespace": selected}]},
                    {"kind": "ServiceAccount", "name": "podgrove-reaper", "namespace": selected,
                     "status": "missing", "warning": "No object with this name was observed"},
                    {"kind": "Role", "name": "podgrove-reaper", "namespace": selected,
                     "status": "inaccessible", "warning": "Read permissions are unavailable"},
                ] if selected else [], "note": "Observed bindings do not prove effective access."},
                "bootstrap": {"status": "reference_only", "namespace": selected,
                              "note": "The bootstrap manifests are a reference, not proof of installation."}, "warnings": []}


class FixtureLogStream:
    """A controllable inert producer behind the production authenticated HTTP handler."""

    def __init__(self, records, source, service, tail):
        self.stopped = threading.Event()
        self.records = deepcopy(records)
        self.source, self.service, self.tail = source, service, tail
        self.pending = queue.Queue()
        self.reason = None

    def start(self):
        return {"type": "start", "source": self.source, "service": self.service, "tail": self.tail,
                "containers": [], "max_seconds": 300, "gap_possible": True, "resume": "restart_with_tail"}

    def events(self):
        for record in self.records:
            if self.stopped.is_set():
                return
            yield record
            if record.get("type") == "end":
                return
        while not self.stopped.wait(0.05):
            try:
                record = self.pending.get_nowait()
            except queue.Empty:
                record = {"type": "heartbeat"}
            yield record
            if record.get("type") == "end":
                return

    def close(self, reason="client_cancelled"):
        if not self.stopped.is_set():
            self.reason = reason
            self.stopped.set()


@pytest.fixture
def dashboard():
    from podgrove.web import DashboardServer
    backend = FixtureDashboard()
    server = DashboardServer("fixture:development", backend=backend)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    worker.start()
    try:
        yield server, backend
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive()


def open_dashboard(page, server):
    page.goto(server.url)
    expect(page.locator("#environment-name")).to_have_text("checkout-api")
    expect(page.locator("#service-total")).not_to_be_empty()
    expect(page.locator("#environment")).to_have_attribute("aria-busy", "false")


def choose_tab(page, name):
    tab = page.get_by_role("tab", name=name, exact=True)
    tab.click()
    expect(tab).to_have_attribute("aria-selected", "true")


def open_configuration(page, view="Worktree configuration"):
    if not page.get_by_role("button", name="Configuration", exact=True).is_visible():
        page.get_by_role("button", name="Worktrees", exact=True).click()
    page.get_by_role("button", name="Configuration", exact=True).click()
    expect(page.locator("#settings-page")).to_be_visible()
    expect(page.locator("#settings-page")).to_have_attribute("aria-busy", "false")
    choose_tab(page, view)


def return_to_worktree(page, ident=IDENT):
    if page.viewport_size["width"] < 768:
        page.get_by_role("button", name="Worktrees", exact=False).click()
    page.locator(f'#worktrees button[data-identity="{ident}"]').click()
    expect(page.locator("#environment")).to_be_visible()


def assert_contained(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Page exceeds its viewport"
    for selector in ("main", ".topbar", ".tabs", ".summary", ".log-controls"):
        for element in page.locator(selector).all():
            if element.is_visible():
                box = element.bounding_box()
                assert box and box["x"] >= -1 and box["x"] + box["width"] <= page.viewport_size["width"] + 1, selector


def test_worktree_search_selection_services_storage_engine_and_readonly_requests(page, dashboard):
    server, backend = dashboard
    original = deepcopy(backend.snapshots)
    requests = []
    page.on("request", lambda request: requests.append((request.method, request.url)))
    open_dashboard(page, server)
    expect(page.locator("#worktree-count")).to_have_text("2")
    expect(page.locator("#services")).to_contain_text("unhealthy")
    page.get_by_role("searchbox", name="Find a worktree").fill("payments")
    expect(page.locator("#worktrees").get_by_role("button")).to_have_count(1)
    page.locator("#worktrees").get_by_role("button").click()
    expect(page.locator("#environment-name")).to_have_text("payments-worker")
    expect(page.locator("#environment-state")).to_contain_text("Disconnected", ignore_case=True)
    expect(page.locator("#detail-warnings")).to_contain_text("local snapshot")
    page.get_by_role("searchbox", name="Find a worktree").fill("no-matching-worktree")
    expect(page.locator("#worktrees").get_by_role("button")).to_have_count(0)
    page.get_by_role("searchbox", name="Find a worktree").fill("checkout-api")
    page.locator("#worktrees").get_by_role("button").click()
    choose_tab(page, "Storage")
    expect(page.locator("#storage")).to_contain_text(f"pg-{IDENT}")
    expect(page.locator("#storage")).to_contain_text("20Gi")
    expect(page.locator("#storage")).to_contain_text("gp3")
    expect(page.locator("#panel-storage")).to_contain_text("disk usage is not measured")
    choose_tab(page, "Engine")
    expect(page.locator("#engine")).to_contain_text(f"pg-{IDENT}-0")
    expect(page.locator("#engine")).to_contain_text("development-node-a")
    expect(page.locator("#engine")).to_contain_text("2Gi")
    assert backend.snapshots == original
    assert requests and all(method == "GET" and url.startswith(server.origin + "/") for method, url in requests)
    assert all(server.token not in url for _, url in requests)


@pytest.mark.parametrize("sync_state,expected", [
    ("reconnecting", "Reconnecting"), ("disconnected", "Paused — inspect the mirror, then run podgrove up --refresh"),
])
def test_sync_recovery_is_visible_without_hiding_healthy_services(page, dashboard, sync_state, expected):
    server, backend = dashboard
    backend.snapshots[IDENT].update(status="degraded", sync_status={
        "state": sync_state, "attempts": 2, "next_retry_at": 1750000500,
        "error": UNSAFE_LOG, "checked_at": 1750000400})
    open_dashboard(page, server)
    expect(page.locator("#running-count")).to_have_text("2 / 2")
    expect(page.locator("#environment-state")).to_contain_text("degraded", ignore_case=True)
    choose_tab(page, "Engine")
    expect(page.locator("#engine")).to_contain_text("File sync")
    expect(page.locator("#engine")).to_contain_text(expected)
    expect(page.locator("#engine")).to_contain_text(UNSAFE_LOG)
    expect(page.locator("#engine")).to_contain_text(f"pg-{IDENT}-0")
    assert page.evaluate("window.__log_xss") is None
    assert not any(call[0] not in ("environments", "detail") for call in backend.calls)


def test_service_logs_tail_wrap_copy_and_xss_are_plain_text(page, dashboard):
    server, backend = dashboard
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=server.origin)
    open_dashboard(page, server)
    api_row = page.locator("#services tr").filter(has=page.get_by_text("api", exact=True))
    api_row.get_by_role("button", name="View api logs", exact=True).click()
    expect(page.get_by_role("tab", name="Logs", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.locator("#log-output")).to_contain_text(UNSAFE_LOG)
    expect(page.locator("#log-output img, #log-output script")).to_have_count(0)
    assert page.evaluate("window.__log_xss === undefined")
    page.get_by_role("combobox", name=re.compile(r"^Lines")).select_option("200")
    page.get_by_role("button", name="Refresh logs", exact=True).click()
    expect(page.locator("#log-output")).to_contain_text("api ready")
    expect(page.locator("#refresh-logs")).to_have_attribute("aria-busy", "false")
    assert ("logs", IDENT, "service", "api", 200) in backend.calls
    page.get_by_label("Wrap lines", exact=True).uncheck()
    expect(page.locator("#log-output")).not_to_have_class(re.compile(r"\bwrap\b"))
    assert_contained(page)
    page.get_by_label("Wrap lines", exact=True).check()
    page.get_by_role("button", name="Copy logs", exact=True).click()
    assert page.evaluate("navigator.clipboard.readText()") == backend.log_text
    page.get_by_label("Source", exact=True).select_option(label="Engine pod")
    page.get_by_role("button", name="Refresh logs", exact=True).click()
    expect(page.locator("#refresh-logs")).to_have_attribute("aria-busy", "false")
    assert ("logs", IDENT, "engine", None, 200) in backend.calls


def test_tab_keyboard_navigation_and_session_token_survive_reload(page, dashboard):
    server, backend = dashboard
    open_dashboard(page, server)
    services = page.get_by_role("tab", name="Services", exact=True)
    services.focus()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("tab", name="Endpoints", exact=True)).to_be_focused()
    expect(page.get_by_role("tab", name="Endpoints", exact=True)).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("tab", name="Storage", exact=True)).to_be_focused()
    expect(page.get_by_role("tab", name="Storage", exact=True)).to_have_attribute("aria-selected", "true")
    page.keyboard.press("End")
    expect(page.get_by_role("tab", name="Engine", exact=True)).to_be_focused()
    page.keyboard.press("Home")
    expect(services).to_be_focused()
    assert "token=" not in page.url
    assert page.evaluate("Object.values(sessionStorage)").count(server.token) == 1
    before = backend.calls.count(("environments",))
    page.reload()
    expect(page.locator("#environment-name")).to_have_text("checkout-api")
    expect(page.locator("#global-error")).to_be_hidden()
    assert backend.calls.count(("environments",)) > before


def test_inventory_error_can_retry_and_empty_state_is_actionable(page, dashboard):
    server, backend = dashboard
    backend.fail_inventory = True
    page.goto(server.url)
    expect(page.locator("#global-error")).to_contain_text("Fixture inventory temporarily unavailable")
    backend.fail_inventory = False
    backend.snapshots.clear()
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#global-error")).to_be_hidden()
    expect(page.locator("#welcome")).to_be_visible()
    expect(page.locator("#start-hint")).to_have_text("podgrove up")
    expect(page.locator("#environment")).to_be_hidden()
    backend.snapshots[IDENT] = snapshot()
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#environment-name")).to_have_text("checkout-api")


def test_detail_and_log_failures_have_visible_recovery(page, dashboard):
    server, backend = dashboard
    backend.fail_detail = True
    page.goto(server.url)
    expect(page.locator("#detail-error")).to_contain_text("Fixture detail temporarily unavailable")
    backend.fail_detail = False
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#detail-error")).to_be_hidden()
    backend.fail_logs = True
    choose_tab(page, "Logs")
    page.get_by_label("Source", exact=True).select_option(label="Engine pod")
    page.get_by_role("button", name="Refresh logs", exact=True).click()
    expect(page.locator("#log-error")).to_contain_text("Fixture logs temporarily unavailable")
    backend.fail_logs = False
    page.get_by_role("button", name="Refresh logs", exact=True).click()
    expect(page.locator("#log-error")).to_be_hidden()
    expect(page.locator("#log-output")).to_contain_text("api ready")


def test_missing_browser_token_reads_no_environment_data(page, dashboard):
    server, backend = dashboard
    page.goto(server.origin)
    expect(page.locator("#global-error")).to_contain_text("Open the browser link printed by podgrove web")
    assert backend.calls == []
    expect(page.locator("#environment")).to_be_hidden()
    open_dashboard(page, server)
    expect(page.locator("#global-error")).to_be_hidden()


def test_worktree_names_and_paths_are_literal_text(page, dashboard):
    server, backend = dashboard
    backend.snapshots[IDENT]["name"] = UNSAFE_LOG
    backend.snapshots[IDENT]["root"] = "/worktrees/" + UNSAFE_LOG
    page.goto(server.url)
    expect(page.locator("#environment-name")).to_have_text(UNSAFE_LOG)
    expect(page.locator("#environment-root")).to_contain_text(UNSAFE_LOG)
    expect(page.locator("#environment-name img, #environment-root img, #worktrees img")).to_have_count(0)
    assert page.evaluate("window.__log_xss === undefined")


@pytest.mark.parametrize("width", [320, 375, 414, 768, 1440])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_responsive_views_and_mobile_worktree_navigation(page, dashboard, width, theme):
    server, backend = dashboard
    page.emulate_media(color_scheme=theme)
    page.set_viewport_size({"width": width, "height": 1000 if width >= 768 else 860})
    open_dashboard(page, server)
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    if width < 768:
        toggle = page.get_by_role("button", name="Worktrees", exact=False)
        expect(toggle).to_have_attribute("aria-expanded", "false")
        toggle.click()
        expect(page.get_by_role("searchbox", name="Find a worktree")).to_be_visible()
        page.get_by_role("searchbox", name="Find a worktree").fill("checkout-api")
        page.locator("#worktrees").get_by_role("button").click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
    else:
        expect(page.get_by_role("button", name="Worktrees", exact=True)).to_be_hidden()
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    for view in ("Services", "Endpoints", "Storage", "Logs", "Engine", "Configuration"):
        if view == "Configuration":
            open_configuration(page)
        else:
            choose_tab(page, view)
        if view == "Logs":
            page.get_by_label("Source", exact=True).select_option(label="Engine pod")
            page.get_by_role("button", name="Refresh logs", exact=True).click()
            expect(page.locator("#log-output")).to_contain_text("api ready")
        assert_contained(page)
        suffix = "-dark" if theme == "dark" else ""
        page.screenshot(path=str(SCREENSHOTS / f"dashboard-{view.lower()}-{width}{suffix}.png"), full_page=True)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_small_text_contrast_and_accessible_control_names(page, dashboard, theme):
    server, _ = dashboard
    page.emulate_media(color_scheme=theme)
    open_dashboard(page, server)
    choose_tab(page, "Logs")
    expect(page.get_by_role("combobox", name="Source", exact=True)).to_be_visible()
    expect(page.get_by_role("combobox", name="Lines", exact=True)).to_be_visible()
    contrasts = page.evaluate("""() => {
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 1;
      const ctx = canvas.getContext('2d', {willReadFrequently:true});
      function rgba(color) {
        ctx.clearRect(0,0,1,1); ctx.fillStyle=color; ctx.fillRect(0,0,1,1);
        return Array.from(ctx.getImageData(0,0,1,1).data);
      }
      function luminance(color) {
        const linear = color.slice(0,3).map(c => {
          c /= 255; return c <= .04045 ? c/12.92 : ((c+.055)/1.055)**2.4;
        });
        return .2126*linear[0]+.7152*linear[1]+.0722*linear[2];
      }
      return ['.sidebar-caption','.worktree-description','#environment-state',
              '#environment-root','#observed','#connection-state','.tab[aria-selected=false]',
              '.log-controls label'].map(selector => {
        const el=document.querySelector(selector); let ancestor=el, bg;
        do { bg=rgba(getComputedStyle(ancestor).backgroundColor); ancestor=ancestor.parentElement; }
        while(bg[3]===0 && ancestor);
        const fg=rgba(getComputedStyle(el).color);
        const a=luminance(fg), b=luminance(bg);
        return {selector,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
      });
    }""")
    assert all(row["ratio"] >= 4.5 for row in contrasts), contrasts


@pytest.mark.parametrize("preferred", ["light", "dark"])
def test_theme_honors_system_preference_and_explicit_choice_survives_reload(page, dashboard, preferred):
    server, backend = dashboard
    page.emulate_media(color_scheme=preferred)
    open_dashboard(page, server)
    expect(page.locator("html")).to_have_attribute("data-theme", preferred)
    changed = "dark" if preferred == "light" else "light"
    before = deepcopy(backend.calls)
    page.get_by_role("button", name=f"Switch to {changed} theme", exact=True).click()
    expect(page.locator("html")).to_have_attribute("data-theme", changed)
    assert page.evaluate("localStorage.getItem('podgrove.web.theme')") == changed
    assert backend.calls == before  # Theme changes do not read or mutate an environment.
    page.reload()
    expect(page.locator("#environment-name")).to_have_text("checkout-api")
    expect(page.locator("html")).to_have_attribute("data-theme", changed)
    expect(page.get_by_role("button", name=f"Switch to {preferred} theme", exact=True)).to_be_visible()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_sidebar_collapses_expands_and_preserves_preference_without_environment_reads(page, dashboard, theme):
    server, backend = dashboard
    page.emulate_media(color_scheme=theme)
    open_dashboard(page, server)
    before = deepcopy(backend.calls)
    original_width = page.locator(".content-shell").bounding_box()["width"]
    page.get_by_role("button", name="Collapse sidebar", exact=True).click()
    toggle = page.get_by_role("button", name="Expand sidebar", exact=True)
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_role("searchbox", name="Find a worktree")).to_be_hidden()
    assert page.locator(".content-shell").bounding_box()["width"] > original_width + 100
    assert backend.calls == before
    assert_contained(page)
    page.reload()
    expect(page.locator("#environment-name")).to_have_text("checkout-api")
    expect(toggle).to_be_visible()
    page.set_viewport_size({"width": 375, "height": 860})
    expect(toggle).to_be_hidden()
    page.get_by_role("button", name="Worktrees", exact=False).click()
    expect(page.get_by_role("searchbox", name="Find a worktree")).to_be_visible()
    page.set_viewport_size({"width": 1440, "height": 1000})
    expect(toggle).to_be_visible()
    toggle.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Collapse sidebar", exact=True)).to_have_attribute("aria-expanded", "true")
    expect(page.get_by_role("searchbox", name="Find a worktree")).to_be_visible()
    page.reload()
    expect(page.get_by_role("button", name="Collapse sidebar", exact=True)).to_be_visible()


@pytest.mark.parametrize("width", [320, 1440])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_endpoints_support_many_forwards_filter_and_refresh(page, dashboard, width, theme):
    server, backend = dashboard
    backend.snapshots[IDENT]["ports"] = [
        {"service": f"service-{index:03}", "target": 8000 + index, "local": 49000 + index}
        for index in range(100)
    ]
    page.set_viewport_size({"width": width, "height": 1000})
    page.emulate_media(color_scheme=theme)
    open_dashboard(page, server)
    expect(page.locator("#endpoint-list")).to_be_hidden()
    choose_tab(page, "Endpoints")
    expect(page.locator("#endpoint-total")).to_have_text("100 endpoints")
    expect(page.locator("#endpoint-list tbody tr")).to_have_count(100)
    expect(page.locator("#endpoint-list a")).to_have_count(0)
    before = deepcopy(backend.calls)
    search = page.get_by_role("searchbox", name="Filter endpoints", exact=True)
    search.fill("service-099")
    expect(page.locator("#endpoint-total")).to_have_text("1 of 100 endpoints")
    expect(page.locator("#endpoint-list")).to_contain_text("127.0.0.1:49099")
    search.fill("8098")
    expect(page.locator("#endpoint-list tbody tr")).to_have_count(1)
    expect(page.locator("#endpoint-list")).to_contain_text("service-098")
    search.fill("127.0.0.1:49097")
    expect(page.locator("#endpoint-list")).to_contain_text("service-097")
    assert backend.calls == before
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#environment")).to_have_attribute("aria-busy", "false")
    expect(search).to_have_value("127.0.0.1:49097")
    expect(page.get_by_role("tab", name="Endpoints", exact=True)).to_have_attribute("aria-selected", "true")
    search.fill("no match")
    expect(page.locator("#endpoint-list")).to_contain_text("No endpoints match this filter")
    search.fill("")
    expect(page.locator("#endpoint-list tbody tr")).to_have_count(100)
    assert_contained(page)


def test_endpoint_empty_state_and_worktree_switch_clear_filter_and_render_literal_names(page, dashboard):
    server, backend = dashboard
    backend.snapshots[IDENT]["ports"][0]["service"] = UNSAFE_LOG
    backend.snapshots[SECOND]["ports"] = []
    open_dashboard(page, server)
    choose_tab(page, "Endpoints")
    expect(page.locator("#endpoint-list")).to_contain_text(UNSAFE_LOG)
    expect(page.locator("#endpoint-list img, #endpoint-list script")).to_have_count(0)
    page.get_by_role("searchbox", name="Filter endpoints").fill("some-filter")
    page.locator("#worktrees").get_by_role("button", name=re.compile("payments-worker")).click()
    expect(page.locator("#environment-name")).to_have_text("payments-worker")
    choose_tab(page, "Endpoints")
    expect(page.locator("#endpoint-total")).to_have_text("0 endpoints")
    expect(page.locator("#endpoint-list")).to_contain_text("No local port forwards are configured")
    expect(page.get_by_role("searchbox", name="Filter endpoints")).to_have_value("")
    expect(page.get_by_role("searchbox", name="Filter endpoints")).to_be_disabled()
    assert page.evaluate("window.__log_xss === undefined")


def test_current_configuration_is_distinct_from_running_data_and_refreshes_without_writes(page, dashboard):
    server, backend = dashboard
    open_dashboard(page, server)
    original = deepcopy(backend.snapshots)
    open_configuration(page)
    expect(page.locator("#configuration")).to_contain_text("podgrove.yml")
    expect(page.locator("#configuration")).to_contain_text("8 hours")
    expect(page.locator("#configuration")).to_contain_text("compose.yml, compose.dev.yml")
    expect(page.locator("#settings-page")).to_contain_text("may differ from the running environment")
    page.get_by_text("Configured port forwards", exact=True).click()
    expect(page.locator("#configuration")).to_contain_text("Container 8000 → available local port")
    assert backend.snapshots == original
    backend.snapshots[IDENT]["configuration"]["settings"]["size"] = "large"
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#configuration")).to_contain_text("large")
    expect(page.get_by_role("button", name="Configuration", exact=True)).to_have_attribute("aria-pressed", "true")
    return_to_worktree(page)
    choose_tab(page, "Engine")
    expect(page.locator("#engine")).to_contain_text("2Gi")


def test_configured_cluster_target_refresh_does_not_replace_observed_scope(page, dashboard):
    server, backend = dashboard
    open_dashboard(page, server)
    open_configuration(page)
    configured = page.locator("#configuration")
    observed = page.locator("#cluster-settings")
    expect(configured).to_contain_text("Configured context")
    expect(configured).to_contain_text("configured:next-cluster")
    expect(configured).to_contain_text("Configured namespace")
    expect(configured).to_contain_text("wt-next")
    expect(observed).to_contain_text("Observed context")
    expect(observed).to_contain_text("fixture:development")
    expect(observed).to_contain_text("Observed namespace")
    expect(observed).to_contain_text("podgrove-testing")
    backend.snapshots[IDENT]["configuration"]["settings"]["cluster"] = {"context": "configured:changed", "namespace": None}
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(configured).to_contain_text("configured:changed")
    expect(configured).to_contain_text("Not configured in this file")
    expect(observed).to_contain_text("fixture:development")
    expect(observed).to_contain_text("podgrove-testing")
    expect(observed).not_to_contain_text("configured:changed")
    assert all(call[0] in ("environments", "detail", "settings") for call in backend.calls)


def test_configured_network_refresh_does_not_change_saved_network_or_claim_enforcement(page, dashboard):
    server, backend = dashboard
    env = backend.snapshots[IDENT]
    env["network"] = {"blocked_cidrs": ["44.55.0.0/16"]}
    env["configuration"]["settings"]["network"] = {"blocked_cidrs": ["55.66.0.0/16", "2001:db8::/32"]}
    original = deepcopy(backend.snapshots)
    open_dashboard(page, server)
    open_configuration(page)
    configured = page.locator("#configuration .detail-list > div").filter(has=page.get_by_text("Configured extra blocked CIDRs", exact=True))
    saved = page.locator("#configuration .detail-list > div").filter(has=page.get_by_text("Saved extra blocked CIDRs", exact=True))
    expect(configured).to_contain_text("55.66.0.0/16, 2001:db8::/32")
    expect(configured).not_to_contain_text("44.55.0.0/16")
    expect(saved).to_contain_text("44.55.0.0/16")
    expect(page.locator("#configuration")).to_contain_text("does not inspect policy enforcement")
    assert backend.snapshots == original
    env["configuration"]["settings"]["network"] = {"blocked_cidrs": []}
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(configured).to_contain_text("None configured")
    expect(saved).to_contain_text("44.55.0.0/16")
    env.pop("network")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(saved).to_contain_text("Not recorded")
    assert all(call[0] in ("environments", "detail", "settings") for call in backend.calls)


@pytest.mark.parametrize("mode,configured_mode,namespace", [
    ("shared", "shared", "team-development"),
    ("worktree", "worktree", "team-development-012345abcdef"),
])
def test_custom_namespace_and_storage_configuration_preserve_observed_scope(page, dashboard, mode, configured_mode, namespace):
    server, backend = dashboard
    backend.snapshots.pop(SECOND)
    env = backend.snapshots[IDENT]
    env["namespace"], env["namespace_mode"] = namespace, mode
    env["configuration"]["settings"]["cluster"] = {
        "context": "configured:custom", "namespace": "team-development",
        "namespace_mode": configured_mode, "storage_class": "team-ssd",
    }
    open_dashboard(page, server)
    expect(page.locator("#environment-namespace")).to_have_text(namespace)
    expect(page.locator("#namespace-mode")).to_have_text(mode)
    open_configuration(page)
    expect(page.locator("#cluster-settings")).to_contain_text(namespace)
    choose_tab(page, "Cluster & namespace")
    expect(page.get_by_role("combobox", name="Configuration namespace", exact=True)).to_have_value(namespace)
    expect(page.locator("#configuration")).to_contain_text("Configured storage class")
    expect(page.locator("#configuration")).to_contain_text("team-ssd")
    expect(page.locator("#configuration")).to_contain_text("Configured namespace mode")
    expect(page.locator("#configuration")).to_contain_text(configured_mode)
    expect(page.locator("#configuration")).not_to_contain_text("podgrove-testing")
    assert ("settings", namespace) in backend.calls


@pytest.mark.parametrize("status", ["missing", "unavailable"])
def test_configuration_read_failure_keeps_worktree_data_available(page, dashboard, status):
    server, backend = dashboard
    backend.snapshots[IDENT]["configuration"] = {
        "source": "current_file", "status": status, "file": "podgrove.yml", "settings": None,
        "warning": "Configuration file is unavailable" if status == "unavailable" else "Configuration file is missing",
    }
    open_dashboard(page, server)
    open_configuration(page)
    expect(page.locator("#configuration")).to_contain_text(backend.snapshots[IDENT]["configuration"]["warning"])
    expect(page.locator("#configuration .detail-list")).to_have_count(0)
    return_to_worktree(page)
    choose_tab(page, "Services")
    expect(page.locator("#services")).to_contain_text("api")
    expect(page.locator("#observed")).to_contain_text("Live observation")


def test_configuration_automatic_and_disabled_forwarding_and_untrusted_text(page, dashboard):
    server, backend = dashboard
    settings = backend.snapshots[IDENT]["configuration"]["settings"]
    settings["compose"] = {"files": None, "profiles": [UNSAFE_LOG], "project_directory": "."}
    settings["forward"] = None
    settings["cluster"] = {"context": UNSAFE_LOG, "namespace": None}
    settings["node_mode"] = "tainted"
    settings["tainted_nodes"] = {"selector": {"pool": "development"}, "taint": {
        "key": "isolation", "value": "development", "effect": "NoSchedule"}}
    open_dashboard(page, server)
    open_configuration(page)
    expect(page.locator("#configuration")).to_contain_text("Automatic discovery")
    expect(page.locator("#configuration")).to_contain_text("Automatic from published Compose ports")
    expect(page.locator("#configuration")).to_contain_text("pool=development")
    expect(page.locator("#configuration")).to_contain_text("isolation=development · NoSchedule")
    expect(page.locator("#configuration")).to_contain_text(UNSAFE_LOG)
    expect(page.locator("#configuration img, #configuration script")).to_have_count(0)
    settings["forward"] = []
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#configuration")).to_contain_text("Disabled by an empty forward list")
    assert page.evaluate("window.__log_xss === undefined")


def test_sidebar_configuration_shows_observed_access_and_remains_available_when_collapsed(page, dashboard):
    server, backend = dashboard
    original = deepcopy(backend.snapshots)
    requests = []
    page.on("request", lambda request: requests.append((request.method, request.url)))
    open_dashboard(page, server)
    page.get_by_role("button", name="Collapse sidebar", exact=True).click()
    open_configuration(page)
    expect(page.locator("#environment")).to_be_hidden()
    expect(page.locator("#cluster-settings")).to_contain_text("fixture:development")
    expect(page.locator("#namespace-details")).to_contain_text("podgrove-bootstrap")
    choose_tab(page, "Service accounts & access")
    role = page.locator(".access-object").filter(has=page.locator("summary", has_text="Role · podgrove-client"))
    role.locator("summary").click()
    expect(role).to_contain_text("pods/log")
    expect(role).to_contain_text("get, list")
    account = page.locator(".access-object").filter(has=page.locator("summary", has_text="ServiceAccount · podgrove-client"))
    account.locator("summary").click()
    expect(account).to_contain_text("Automount token")
    expect(account).to_contain_text("false")
    binding = page.locator(".access-object").filter(has=page.locator("summary", has_text="RoleBinding · podgrove-client"))
    binding.locator("summary").click()
    expect(binding).to_contain_text("ServiceAccount · podgrove-testing/podgrove-client")
    expect(page.locator("#access-details")).to_contain_text("missing")
    expect(page.locator("#access-details")).to_contain_text("inaccessible")
    expect(page.locator("#access-details")).to_contain_text("not proof of installation")
    page.get_by_role("button", name="Expand sidebar", exact=True).click()
    return_to_worktree(page)
    expect(page.get_by_role("button", name="Configuration", exact=True)).to_have_attribute("aria-pressed", "false")
    assert backend.snapshots == original
    assert requests and all(method == "GET" and url.startswith(server.origin + "/") for method, url in requests)


def test_configuration_worktree_and_namespace_selection_and_error_retry(page, dashboard):
    server, backend = dashboard
    backend.snapshots[SECOND]["namespace"] = "default"
    backend.snapshots[SECOND]["configuration"]["settings"]["size"] = "large"
    open_dashboard(page, server)
    backend.fail_settings = True
    open_configuration(page)
    expect(page.locator("#settings-error")).to_contain_text("Fixture configuration temporarily unavailable")
    expect(page.locator("#configuration")).to_contain_text("podgrove.yml")
    backend.fail_settings = False
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#settings-error")).to_be_hidden()
    expect(page.locator("#settings-page")).to_have_attribute("aria-busy", "false")
    page.get_by_role("combobox", name="Configuration worktree", exact=True).select_option(SECOND)
    expect(page.locator("#configuration")).to_contain_text("large")
    choose_tab(page, "Cluster & namespace")
    expect(page.get_by_role("combobox", name="Configuration namespace", exact=True)).to_have_value("default")
    expect(page.locator("#environment")).to_be_hidden()
    choose_tab(page, "Cluster & namespace")
    page.get_by_role("combobox", name="Configuration namespace", exact=True).select_option("podgrove-testing")
    expect(page.locator("#settings-page")).to_have_attribute("aria-busy", "false")
    assert ("settings", "podgrove-testing") in backend.calls
    assert ("settings", "default") in backend.calls


def test_configuration_page_without_worktrees_and_endpoint_truncation_notice(page, dashboard):
    server, backend = dashboard
    backend.snapshots[IDENT]["ports_truncated"] = True
    backend.snapshots[IDENT]["ports_omitted"] = 12
    open_dashboard(page, server)
    choose_tab(page, "Endpoints")
    expect(page.locator("#endpoint-total")).to_contain_text("shown · 12 omitted")
    backend.snapshots.clear()
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#welcome")).to_be_visible()
    open_configuration(page)
    expect(page.locator("#cluster-settings")).to_contain_text("fixture:development")
    expect(page.locator("#configuration")).to_contain_text("No saved worktrees")
    expect(page.locator("#namespace-details")).to_contain_text("cluster.namespace")
    expect(page.get_by_role("combobox", name="Configuration worktree", exact=True)).to_be_disabled()
    choose_tab(page, "Cluster & namespace")
    expect(page.get_by_role("combobox", name="Configuration namespace", exact=True)).to_be_disabled()


@pytest.mark.parametrize("forward", ["ready", "reconnecting", "disconnected", "unknown"])
def test_endpoint_tab_separates_forward_state_from_running_services(page, dashboard, forward):
    server, backend = dashboard
    backend.snapshots[IDENT]["ports"][0]["status"] = forward
    backend.snapshots[IDENT]["status"] = "ready" if forward == "ready" else "degraded"
    open_dashboard(page, server)
    choose_tab(page, "Endpoints")
    expect(page.locator("#endpoint-list tbody tr").first).to_contain_text(forward)
    expect(page.locator("#endpoint-list tbody tr").first).to_contain_text("127.0.0.1:49152")


def start_live(page, server, source="engine"):
    open_dashboard(page, server)
    choose_tab(page, "Logs")
    page.get_by_label("Source", exact=True).select_option(source)
    expect(page.locator("#refresh-logs")).to_have_attribute("aria-busy", "false")
    page.get_by_role("button", name="Go live", exact=True).click()
    expect(page.locator("#log-status")).to_contain_text("Live · receiving")


def test_compact_identity_and_custom_budgets_keep_configured_and_observed_separate(page, dashboard):
    server, backend = dashboard
    env = backend.snapshots[IDENT]
    settings = env["configuration"]["settings"]
    settings.update(resources_mode="custom", resources={"requests": {"cpu": "0", "memory": "6Gi"},
                                                        "limits": {"cpu": "6", "memory": "12Gi", "ephemeral-storage": "8Gi"}},
                    init_resources={"requests": {"cpu": "0"}, "limits": {"memory": "64Mi"}},
                    storage={"size": "80Gi"})
    env["engine"]["init_containers"] = [{"name": "storage-init", "resources": {"limits": {"memory": "32Mi"}}}]
    original = deepcopy(env)
    open_dashboard(page, server)
    expect(page.locator(".topbar #environment-name")).to_have_text("checkout-api")
    expect(page.locator(".summary #context")).to_have_text("fixture:development")
    expect(page.locator("#environment-branch")).to_have_text("feature/review-dashboard")
    expect(page.locator("#environment-worktree")).to_have_text("checkout-api")
    expect(page.locator("#environment-root")).to_be_hidden()
    page.get_by_text("View path", exact=True).click()
    expect(page.locator("#environment-root")).to_be_visible()
    expect(page.locator("#environment-root")).to_have_text(env["root"])
    positions = page.locator(".summary > div").evaluate_all("items => items.map(x => Math.round(x.getBoundingClientRect().y))")
    assert len(set(positions)) == 1
    open_configuration(page, "Resources")
    groups = page.locator(".resource-group")
    current = groups.filter(has=page.get_by_role("heading", name="Current file · engine", exact=True))
    observed = groups.filter(has=page.get_by_role("heading", name="Observed engine pod", exact=True))
    expect(current).to_contain_text("Custom budget")
    expect(current).to_contain_text("6Gi")
    expect(current).to_contain_text("12Gi")
    expect(current.get_by_role("cell", name="0", exact=True)).to_have_count(1)
    expect(observed).to_contain_text("2Gi")
    expect(observed).not_to_contain_text("12Gi")
    expect(groups.filter(has=page.get_by_role("heading", name="Current file · persistent storage", exact=True))).to_contain_text("80Gi")
    expect(groups.filter(has=page.get_by_role("heading", name="Observed persistent volume claim", exact=True))).to_contain_text("20Gi")
    expect(page.locator("#resource-configuration")).to_contain_text("Observed initializer · storage-init")
    expect(page.locator("#config-panel-resources")).to_contain_text("do not measure CPU, memory or disk usage")
    assert env == original


@pytest.mark.parametrize("width", [320, 1440])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_single_focus_ring_aligned_controls_and_keyboard_configuration_tabs(page, dashboard, width, theme):
    server, _ = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    page.emulate_media(color_scheme=theme)
    open_dashboard(page, server)
    if width < 768:
        page.get_by_role("button", name="Worktrees", exact=True).click()
    page.get_by_role("searchbox", name="Find a worktree").focus()
    ring = page.locator("#search").evaluate("el => ({inner:getComputedStyle(el).outlineWidth, outer:getComputedStyle(el.parentElement).outlineWidth, offset:getComputedStyle(el.parentElement).outlineOffset, border:getComputedStyle(el.parentElement).borderColor})")
    assert ring["inner"] == "0px" and ring["outer"] == "2px" and ring["offset"] == "0px", ring
    if width >= 768:
        assert page.locator("#sidebar-toggle").bounding_box()["height"] == 32
    choose_tab(page, "Logs")
    page.get_by_label("Source", exact=True).select_option("engine")
    expect(page.locator("#refresh-logs")).to_have_attribute("aria-busy", "false")
    heights = page.locator("#log-source, #log-tail, #refresh-logs, #live-logs").evaluate_all("items => items.map(x => x.getBoundingClientRect().height)")
    assert heights == [44 if width < 768 else 36] * 4
    for wrapper in page.locator(".log-controls .select-wrap").all():
        box, chevron = wrapper.bounding_box(), wrapper.locator("svg").bounding_box()
        assert abs((box["y"] + box["height"] / 2) - (chevron["y"] + chevron["height"] / 2)) < 1
    assert page.locator("#refresh-logs").evaluate("el => getComputedStyle(el).backgroundColor") != "rgba(0, 0, 0, 0)"
    page.keyboard.press("Tab")  # Establish keyboard modality for button focus.
    for selector in ("#log-source", "#log-tail", "#refresh-logs", "#live-logs", "#fullscreen-logs"):
        control = page.locator(selector)
        before = control.bounding_box()
        control.focus()
        appearance = control.evaluate("""el => {
          const s = getComputedStyle(el);
          return {outline:s.outlineWidth, offset:s.outlineOffset, border:s.borderColor,
                  shadow:s.boxShadow, visible:el.matches(':focus-visible')};
        }""")
        assert appearance == {"outline": "2px", "offset": "0px", "border": "rgba(0, 0, 0, 0)",
                              "shadow": "none", "visible": True}, (selector, appearance)
        after = control.bounding_box()
        assert (before["width"], before["height"]) == (after["width"], after["height"])
    page.locator("#log-source").focus()
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.locator(".log-controls").screenshot(path=str(SCREENSHOTS / f"single-border-logs-{theme}-{width}.png"))
    open_configuration(page, "Cluster & namespace")
    page.keyboard.press("Tab")
    for selector in ("#config-worktree", "#settings-namespace"):
        control = page.locator(selector)
        control.focus()
        assert control.evaluate("el => getComputedStyle(el).outlineOffset") == "0px"
        assert control.evaluate("el => getComputedStyle(el).borderColor") == "rgba(0, 0, 0, 0)"
    cluster = page.get_by_role("tab", name="Cluster & namespace", exact=True)
    cluster.focus()
    page.keyboard.press("End")
    resources = page.get_by_role("tab", name="Resources", exact=True)
    expect(resources).to_be_focused()
    expect(page.locator("#config-panel-resources")).to_be_visible()
    expect(page.locator("#config-panel-cluster")).to_be_hidden()
    selected, strip = resources.bounding_box(), page.locator(".settings-tabs").bounding_box()
    assert selected["x"] >= strip["x"] - 1 and selected["x"] + selected["width"] <= strip["x"] + strip["width"] + 1
    page.keyboard.press("Home")
    expect(cluster).to_be_focused()
    assert_contained(page)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_live_logs_authentication_text_safety_pause_resume_and_no_lifecycle_writes(page, dashboard, theme):
    server, backend = dashboard
    page.emulate_media(color_scheme=theme)
    requests = []
    page.on("request", lambda request: requests.append((request.method, request.url, request.headers)))
    original = deepcopy(backend.snapshots)
    start_live(page, server, "service:api")
    expect(page.locator("#log-output")).to_contain_text("live fixture ready 🌿")
    expect(page.locator("#log-output")).to_contain_text(UNSAFE_LOG)
    expect(page.locator("#log-output img, #log-output script")).to_have_count(0)
    assert page.evaluate("window.__log_xss === undefined")
    pause = page.get_by_role("button", name="Pause live", exact=True)
    pause.hover()
    contrast = pause.evaluate("""el => {
      const canvas=document.createElement('canvas'); canvas.width=canvas.height=1;
      const ctx=canvas.getContext('2d',{willReadFrequently:true});
      function luminance(color) {
        ctx.fillStyle=color;ctx.fillRect(0,0,1,1);
        const c=[...ctx.getImageData(0,0,1,1).data].slice(0,3).map(v=>{
          v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;
        });return .2126*c[0]+.7152*c[1]+.0722*c[2];
      }
      const styles=getComputedStyle(el), a=luminance(styles.color), b=luminance(styles.backgroundColor);
      return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
    }""")
    assert contrast >= 4.5
    first = backend.live_streams[-1]
    first.pending.put({"type": "line", "text": "new service line\n", "container": "a" * 64, "stream": "stderr"})
    expect(page.locator("#log-output")).to_contain_text("[aaaaaaaaaaaa · stderr] new service line")
    page.get_by_role("button", name="Pause live", exact=True).click()
    assert first.stopped.wait(2), "Pausing must disconnect the real HTTP follower"
    expect(page.locator("#log-output")).to_contain_text("new service line")
    page.get_by_role("button", name="Resume live", exact=True).click()
    expect(page.locator("#log-status")).to_contain_text("Live · receiving")
    expect(page.locator("#log-output")).not_to_contain_text("new service line")
    expect(page.locator(".log-guidance")).to_contain_text("may be missed or repeated")
    assert len(backend.live_streams) == 2
    streams = [(method, url, headers) for method, url, headers in requests if "/logs/stream?" in url]
    assert len(streams) == 2 and all(headers["x-podgrove-token"] == server.token for _, _, headers in streams)
    assert all(method == "GET" and url.startswith(server.origin + "/") and server.token not in url for method, url, _ in requests)
    assert backend.snapshots == original


@pytest.mark.parametrize("action", ["source", "tail", "tab", "worktree", "configuration", "reload"])
def test_live_follow_is_cancelled_when_its_view_changes(page, dashboard, action):
    server, backend = dashboard
    start_live(page, server)
    stream = backend.live_streams[-1]
    if action == "source":
        page.get_by_label("Source", exact=True).select_option("service:api")
    elif action == "tail":
        page.get_by_label("Lines", exact=True).select_option("200")
    elif action == "tab":
        choose_tab(page, "Services")
    elif action == "worktree":
        page.locator(f'#worktrees button[data-identity="{SECOND}"]').click()
    elif action == "configuration":
        open_configuration(page)
    else:
        page.reload()
    assert stream.stopped.wait(2)
    assert len(backend.live_streams) == 1


@pytest.mark.parametrize("reason,message", [
    ("completed", "source closed"), ("lifetime_limit", "five-minute"),
    ("ownership_changed", "pod or container changed"), ("source_unavailable", "source became unavailable"),
    ("server_shutdown", "Podgrove web stopped"), ("client_cancelled", "connection closed"),
])
def test_live_end_reason_preserves_logs_and_requires_explicit_resume(page, dashboard, reason, message):
    server, backend = dashboard
    start_live(page, server)
    stream = backend.live_streams[-1]
    stream.pending.put({"type": "end", "reason": reason, "gap_possible": True, "partial_lines_discarded": 1})
    expect(page.locator("#log-status")).to_contain_text(message)
    expect(page.locator("#log-output")).to_contain_text("live fixture ready")
    expect(page.locator("#log-notice")).to_contain_text("incomplete line")
    expect(page.get_by_role("button", name="Resume live", exact=True)).to_have_attribute("aria-pressed", "false")
    assert stream.stopped.wait(2) and len(backend.live_streams) == 1


def test_live_http_error_preserves_snapshot_and_snapshot_remains_usable(page, dashboard):
    server, backend = dashboard
    backend.fail_live = True
    open_dashboard(page, server)
    choose_tab(page, "Logs")
    page.get_by_label("Source", exact=True).select_option("engine")
    expect(page.locator("#log-output")).to_contain_text("api ready")
    page.get_by_role("button", name="Go live", exact=True).click()
    expect(page.locator("#log-error")).to_contain_text("Fixture live logs are unavailable")
    expect(page.locator("#log-output")).to_contain_text("api ready")
    page.get_by_role("button", name="Refresh logs", exact=True).click()
    expect(page.locator("#log-error")).to_be_hidden()
    expect(page.locator("#log-output")).to_contain_text("api ready")


@pytest.mark.parametrize("line_size", [10, 300])
def test_live_browser_buffer_has_line_and_byte_bounds(page, dashboard, line_size):
    server, backend = dashboard
    backend.live_records = [{"type": "line", "text": f"line-{index:04d} " + "x" * line_size + "\n", "stream": "stdout"}
                            for index in range(2100)]
    start_live(page, server)
    expect(page.locator("#log-output")).to_contain_text("line-2099")
    expect(page.locator("#log-output")).not_to_contain_text("line-0000")
    expect(page.locator("#log-buffer")).to_contain_text("buffer trimmed")
    bounds = page.locator("#log-output").evaluate("el => ({lines:el.textContent.split('\\n').filter(Boolean).length, bytes:new TextEncoder().encode(el.textContent).length})")
    assert bounds["lines"] <= 2000 and bounds["bytes"] <= 256 * 1024


def test_live_autoscroll_can_pause_and_jump_to_latest(page, dashboard):
    server, backend = dashboard
    backend.live_records = [{"type": "line", "text": f"line {index}\n", "stream": "stdout"} for index in range(120)]
    start_live(page, server)
    expect(page.locator("#log-output")).to_contain_text("line 119")
    output = page.locator("#log-output")
    output.evaluate("el => {el.scrollTop=0}")
    expect(page.get_by_label("Auto-scroll", exact=True)).not_to_be_checked()
    backend.live_streams[-1].pending.put({"type": "line", "text": "latest fixture line\n", "stream": "stdout"})
    expect(output).to_contain_text("latest fixture line")
    assert output.evaluate("el => el.scrollTop") == 0
    page.get_by_role("button", name="Jump to latest", exact=True).click()
    expect(page.get_by_label("Auto-scroll", exact=True)).to_be_checked()
    assert output.evaluate("el => el.scrollHeight-el.scrollTop-el.clientHeight") < 24


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("width", [320, 1440])
def test_live_fullscreen_native_and_fallback_restore_focus_and_keep_stream(page, dashboard, fallback, width):
    server, backend = dashboard
    page.set_viewport_size({"width": width, "height": 900})
    start_live(page, server)
    if fallback:
        page.evaluate("() => { document.querySelector('#panel-logs').requestFullscreen = () => Promise.reject(new Error('fixture denial')); }")
    button = page.get_by_role("button", name="Fullscreen logs", exact=True)
    button.click()
    expect(page.locator("#panel-logs")).to_have_class(re.compile(r"\bfullscreen\b"))
    expect(page.locator("#panel-logs")).to_have_attribute("role", "dialog")
    if not fallback:
        page.wait_for_function("document.fullscreenElement?.id === 'panel-logs'")
    assert page.locator(".topbar").evaluate("el => el.inert")
    backend.live_streams[-1].pending.put({"type": "line", "text": "line during fullscreen\n", "stream": "stdout"})
    expect(page.locator("#log-output")).to_contain_text("line during fullscreen")
    page.get_by_role("button", name="Copy logs", exact=True).focus()
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Exit fullscreen logs", exact=True)).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator("#panel-logs")).not_to_have_class(re.compile(r"\bfullscreen\b"))
    expect(button).to_be_focused()
    assert not page.locator(".topbar").evaluate("el => el.inert")
    assert not backend.live_streams[-1].stopped.is_set()
    expect(page.locator("#log-output")).to_contain_text("line during fullscreen")


def test_many_replicas_offer_exact_container_for_snapshot_and_live_follow(page, dashboard):
    server, backend = dashboard
    service = backend.snapshots[IDENT]["services"][0]
    service["containers"] = [{"id": f"{index + 1:064x}", "name": f"api-{index + 1}", "state": "running"} for index in range(9)]
    service["replicas"] = 9
    open_dashboard(page, server)
    choose_tab(page, "Logs")
    page.get_by_label("Source", exact=True).select_option("service:api")
    page.get_by_role("button", name="Go live", exact=True).click()
    expect(page.locator("#log-error")).to_contain_text("Select one container")
    container = service["containers"][-1]["id"]
    page.get_by_label("Source", exact=True).select_option(f"container:api:{container}")
    expect(page.locator("#refresh-logs")).to_have_attribute("aria-busy", "false")
    assert ("container_logs", IDENT, "api", container) in backend.calls
    page.get_by_role("button", name="Go live", exact=True).click()
    expect(page.locator("#log-status")).to_contain_text("Live · receiving")
    assert ("logs_stream", IDENT, "service", "api", 100, container) in backend.calls
    stream = backend.live_streams[-1]
    service["containers"].pop()
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_label("Source", exact=True)).to_have_value("")
    expect(page.get_by_role("button", name="Go live", exact=True)).to_be_disabled()
    assert stream.stopped.wait(2)


def test_cancelled_fullscreen_request_cannot_enter_late(page, dashboard):
    server, _ = dashboard
    open_dashboard(page, server)
    choose_tab(page, "Logs")
    page.evaluate("""() => {
      const panel=document.querySelector('#panel-logs');
      let current=null;
      Object.defineProperty(document,'fullscreenElement',{configurable:true,get:()=>current});
      document.exitFullscreen=async()=>{current=null;window.fixtureExited=true};
      panel.requestFullscreen=()=>new Promise(resolve=>{
        window.fixtureFinishFullscreen=()=>{current=panel;resolve()};
      });
    }""")
    page.get_by_role("button", name="Fullscreen logs", exact=True).click()
    page.get_by_role("button", name="Exit fullscreen logs", exact=True).click()
    expect(page.locator("#panel-logs")).not_to_have_class(re.compile(r"\bfullscreen\b"))
    page.evaluate("window.fixtureFinishFullscreen()")
    page.wait_for_function("window.fixtureExited === true && document.fullscreenElement === null")
    assert not page.locator(".topbar").evaluate("el=>el.inert")


@pytest.mark.parametrize("source", ["engine", "service:api"])
def test_all_logs_requests_selected_history_for_snapshot_and_live_with_visible_limits(page, dashboard, monkeypatch, source):
    server, backend = dashboard
    original = backend.logs
    def all_snapshot(ident, *, source, service, tail, container=None):
        if tail != "all":
            return original(ident, source=source, service=service, tail=tail, container=container)
        backend.calls.append(("logs", ident, source, service, tail))
        return {"source": source, "service": service, "tail": tail,
                "text": "retained snapshot history\n", "truncated": True}
    monkeypatch.setattr(backend, "logs", all_snapshot)
    backend.live_records = [{"type": "line", "text": f"retained row {index}\n", "stream": "stdout"}
                            for index in range(2105)]
    open_dashboard(page, server)
    choose_tab(page, "Logs")
    page.get_by_label("Source", exact=True).select_option(source)
    expect(page.locator("#refresh-logs")).to_have_attribute("aria-busy", "false")
    page.get_by_label("Lines", exact=True).select_option(label="All logs")
    expect(page.locator("#log-output")).to_have_text("retained snapshot history\n")
    expect(page.locator("#log-status")).to_contain_text("This snapshot is incomplete")
    expect(page.locator(".log-guidance")).to_contain_text("all available retained history for the selected source")
    expect(page.locator(".log-guidance")).to_contain_text("64 KiB")
    expect(page.locator(".log-guidance")).to_contain_text("2,000 lines or 256 KiB")
    service = "api" if source == "service:api" else None
    kind = "service" if service else "engine"
    assert ("logs", IDENT, kind, service, "all") in backend.calls
    page.get_by_role("button", name="Go live", exact=True).click()
    expect(page.locator("#log-status")).to_contain_text("Live · receiving retained history and new lines")
    expect(page.locator("#log-output")).to_contain_text("retained row 2104")
    expect(page.locator("#log-buffer")).to_have_text("2,000 lines · buffer trimmed")
    assert ("logs_stream", IDENT, kind, service, "all", None) in backend.calls
    assert len(page.locator("#log-output").inner_text().splitlines()) == 2000
    assert "retained row 0\n" not in page.locator("#log-output").inner_text()
    stream = backend.live_streams[-1]
    page.get_by_role("button", name="Pause live", exact=True).click()
    assert stream.stopped.wait(2)
    page.get_by_label("Lines", exact=True).select_option("100")
    expect(page.locator("#log-output")).to_contain_text("api ready")
    assert ("logs", IDENT, kind, service, 100) in backend.calls


@pytest.mark.parametrize("connect", [[None], [{"bogus": 1}], [{"name": "db"}, {"name": "cache"}]])
def test_configuration_view_survives_malformed_legacy_connect(page, dashboard, connect):
    server, backend = dashboard
    settings = backend.snapshots[IDENT]["configuration"]["settings"]
    settings["connect"] = connect
    settings["network"] = {"blocked_cidrs": [], "pod_to_pod": "open"}
    open_dashboard(page, server)
    open_configuration(page)
    expect(page.locator("#configuration")).to_contain_text(f"{len(connect)} legacy entr")
    expect(page.locator("#configuration")).to_contain_text("up refuses them")
    expect(page.locator("#configuration")).to_contain_text("open")
