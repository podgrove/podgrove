"""Run inside the isolated wheel environment; no listening socket is created."""
from email.message import Message
import hashlib
import ipaddress
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import yaml

from podgrove import cli, state, web
from podgrove.config import CONFIG_SCHEMA, load_cluster
from podgrove.network import PRIVATE_AND_SPECIAL_IPV4, policy_spec
from podgrove.web_settings import _resources

result = {"schema": CONFIG_SCHEMA, "targets": {}, "configuration": {}, "assets": {}, "checks": {}}
assert "sitecustomize" in __import__("sys").modules
assert "sync" in CONFIG_SCHEMA["properties"]
assert CONFIG_SCHEMA["properties"]["sync"]["properties"]["exclude"]["type"] == "array"
root = Path.cwd()
for mode in ("shared", "worktree"):
    base = "package-" + mode
    config = {"cluster": {"context": "package:offline", "namespace": base, "namespace_mode": mode,
                          "storage_class": "package-delete-sc"},
              "network": {"blocked_cidrs": ["8.8.8.0/24", "2001:db8:abcd::/48"]},
              "sync": {"exclude": ["node_modules", ".venv"]},
              "compose": {"files": ["missing-compose.yaml"], "env_file": "missing.env"}}
    (root / "podgrove.yml").write_text(yaml.safe_dump(config))
    selected = {}
    def fake_serve(context, **kwargs):
        selected.update(context=context, **kwargs)
        return 0
    web.serve = fake_serve
    assert cli.execute(cli.parser().parse_args(["web", "--no-open"])) == 0
    namespace = base if mode == "shared" else f"{base}-wt-{state.identity(root)}"
    assert selected == {"context": "package:offline", "port": 0, "namespace": namespace, "open_browser": False}
    assert load_cluster(root) == config["cluster"]
    result["targets"][mode] = selected
    configuration = web.configuration_metadata({"root": str(root)})
    assert configuration["status"] == "available" and configuration["source"] == "current_file"
    assert configuration["settings"]["network"] == config["network"]
    assert configuration["settings"]["sync"] == config["sync"]
    assert "missing.env" not in json.dumps(configuration)
    result["configuration"][mode] = {"source": configuration["source"], "status": configuration["status"],
                                    "network": configuration["settings"]["network"]}
result["checks"]["bare_web_config_target_resolution_without_flags"] = True
result["checks"]["safe_configuration_projection_preserves_blocked_cidrs_and_sync_excludes"] = True
policy = policy_spec(state.identity(root), config["network"])
ipblocks = [peer["ipBlock"] for rule in policy["egress"] for peer in rule.get("to", []) if "ipBlock" in peer]
assert len(ipblocks) == 1 and ipblocks[0]["cidr"] == "0.0.0.0/0"
exclusions = [ipaddress.ip_network(value) for value in ipblocks[0]["except"]]
for value in [*PRIVATE_AND_SPECIAL_IPV4, "8.8.8.0/24"]:
    assert any(ipaddress.ip_network(value).subnet_of(parent) for parent in exclusions)
assert policy["ingress"] == []
result["checks"]["installed_engine_policy_keeps_builtin_and_configured_exclusions"] = True

namespace = "package-shared"
backend = web.Dashboard("package:offline", namespace)
assert backend.environments()["environments"] == []
reads = []
def fake_read(command, **kwargs):
    reads.append(command)
    assert command[:3] == ["kubectl", "--context", "package:offline"]
    assert command[command.index("--namespace") + 1] == namespace
    assert "get" in command and "--ignore-not-found" in command
    assert (command[7], command[8]) in {(r.resource, r.name) for r in _resources(namespace)}
    return b""
web.bounded_read_command = fake_read
server = SimpleNamespace(origin="http://127.0.0.1:12345", token="isolated-package-token",
                         static_dir=Path(web.__file__).with_name("web_static"), backend=backend,
                         request_slots=threading.BoundedSemaphore(4))

def request(path, *, authorized=True, method="GET", origin=None):
    handler = object.__new__(web.DashboardHandler)
    handler.server = server
    handler.path = path
    handler.headers = Message()
    handler.headers["Host"] = "127.0.0.1:12345"
    if authorized:
        handler.headers["X-Podgrove-Token"] = server.token
    if origin:
        handler.headers["Origin"] = origin
    handler.wfile = io.BytesIO()
    response = {"headers": {}}
    handler.send_response = lambda code: response.update(status=code)
    handler.send_header = lambda key, value: response["headers"].update({key: value})
    handler.end_headers = lambda: None
    getattr(handler, "do_" + method)()
    response["body"] = handler.wfile.getvalue()
    return response

for path, (name, content_type) in web.STATIC.items():
    response = request(path, authorized=False)
    raw = (server.static_dir / name).read_bytes()
    assert response["status"] == 200 and response["body"] == raw
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["X-Frame-Options"] == "DENY"
    assert response["headers"]["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'self'" in response["headers"]["Content-Security-Policy"]
    assert server.token.encode() not in raw
    result["assets"][name] = hashlib.sha256(raw).hexdigest()
html = (server.static_dir / "index.html").read_bytes()
assert all(marker in html for marker in (b'id="theme-toggle"', b'id="tab-endpoints"', b'id="settings-button"', b'id="settings-page"'))
assert b'data-theme="dark"' in (server.static_dir / "tokens.css").read_bytes()
assert b"podgrove.web.theme" in (server.static_dir / "app.js").read_bytes()
assert request("/api/environments", authorized=False)["status"] == 403
assert request("/api/environments", origin="https://untrusted.invalid")["status"] == 403
assert request("/api/environments", method="POST")["status"] == 405
response = request("/api/environments")
assert response["status"] == 200 and json.loads(response["body"])["environments"] == []
assert not reads
assert request("/api/settings", authorized=False)["status"] == 403
assert not reads
response = request("/api/settings")
observed = json.loads(response["body"])
assert response["status"] == 200 and observed["selected_namespace"] == namespace
assert observed["namespace_options"] == [namespace] and observed["context"] == "package:offline"
assert observed["namespace"] is None and observed["provisioning"]["status"] == "missing"
assert len(reads) == len(_resources(namespace)) == 8
assert len(observed["access"]["objects"]) == 7
assert all(item["status"] == "missing" for item in observed["access"]["objects"])
assert request("/api/settings?namespace=unapproved")["status"] == 400
assert len(reads) == 8
result["checks"]["mock_http_assets_auth_origin_get_only_and_exact_settings_scope"] = True
result["checks"]["no_listener_or_external_service_started"] = True
print(json.dumps(result))
