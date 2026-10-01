"""Offline fixture and fail-closed lifecycle checks for the explicit network lane."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location("check_pod_network", Path(__file__).resolve().parents[1] / "scripts/check_pod_network.py")
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


@pytest.fixture
def args(tmp_path):
    return SimpleNamespace(output=tmp_path / "evidence", podgrove_bin=Path(sys.executable), context="explicit-context",
                           namespace_a="approved-a", namespace_b="approved-b", storage_class="approved-storage",
                           cluster_domain=None, settle_timeout=5)


def marker(namespace="approved-a"):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": acceptance.MARKER, "namespace": namespace, "uid": "captured-bootstrap",
        "labels": {acceptance.base.MANAGED: "podgrove", "podgrove.dev/component": "bootstrap"}},
        "data": {"version": "1", "namespace_mode": "shared"}}


def publishers():
    return {"services": [{"Service": name, "State": "running", "Health": "healthy", "Publishers": [
        {"URL": "0.0.0.0", "TargetPort": ports[0], "PublishedPort": ports[1], "Protocol": "tcp"}]}
        for name, ports in acceptance.PORTS.items()]}


def test_marker_name_matches_real_product_bootstrap():
    from podgrove.bootstrap import PROVISIONING_MARKER
    assert acceptance.MARKER == PROVISIONING_MARKER
    assert acceptance.marker_proof(marker(), "approved-a")["uid"] == "captured-bootstrap"


@pytest.mark.parametrize("change", ["namespace", "name", "uid", "owner", "environment", "deleting", "mode", "version", "kind"])
def test_foreign_or_unprepared_namespace_marker_is_refused(change):
    value = marker()
    if change in ("namespace", "name", "uid"):
        value["metadata"][change] = "" if change == "uid" else "foreign"
    elif change == "owner":
        value["metadata"]["labels"][acceptance.base.MANAGED] = "foreign"
    elif change == "environment":
        value["metadata"]["labels"][acceptance.base.ENVIRONMENT] = "012345abcdef"
    elif change == "deleting":
        value["metadata"]["deletionTimestamp"] = "now"
    elif change in ("mode", "version"):
        value["data"]["namespace_mode" if change == "mode" else "version"] = "foreign"
    else:
        value["kind"] = "Namespace"
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.marker_proof(value, "approved-a")


@pytest.mark.parametrize("value", [None, [], {}, {"metadata": None}])
def test_malformed_marker_is_refused(value):
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.marker_proof(value, "approved-a")


def test_tiny_fixture_separates_exposed_and_private_services_and_no_host_mounts():
    source = acceptance.fixture_compose(b"nonce", target=False)
    assert set(source["services"]) == {"client"}
    model = acceptance.fixture_compose(b"nonce\x00\xff", target=True)
    assert set(model["services"]) == {"client", "app", "private"}
    for name, (target, published) in acceptance.PORTS.items():
        service = model["services"][name]
        assert service["ports"] == [{"target": target, "published": str(published), "host_ip": "0.0.0.0", "protocol": "tcp"}]
        assert service["image"] == "python:3.12-alpine"
        assert not any(key in service for key in ("privileged", "volumes", "network_mode", "build"))
        compile(service["command"][-1], "fixture", "exec")
        compile(service["healthcheck"]["test"][-1], "healthcheck", "exec")


def test_selected_fixture_settings_use_published_port_and_directory_globs():
    assert acceptance.network_config("selected", peer_namespace="approved-b") == {
        "pod_to_pod": "selected", "connect": [{"namespace": "approved-b", "worktree": "apis-*", "ports": [18080]}]}
    assert acceptance.network_config("selected", peer_namespace="approved-a", target=True, worktree="web-*") == {
        "pod_to_pod": "selected", "expose": [{"service": "app", "from": [{"namespace": "approved-a", "worktree": "web-*"}]}]}
    optional = acceptance.network_config("selected", peer_namespace="approved-a", target=True)
    assert optional["expose"][0]["from"] == [{"namespace": "approved-a"}]
    assert acceptance.network_config("disabled") == {"pod_to_pod": "disabled"}
    assert acceptance.network_config("open") == {"pod_to_pod": "open"}


def test_generated_network_modes_follow_real_product_validation():
    from podgrove.network import network_settings, validate_model
    model = acceptance.fixture_compose(b"fixture", target=True)
    for mode in ("disabled", "open", "selected"):
        settings = acceptance.network_config(mode, peer_namespace="approved-a", target=True, worktree="web-*")
        normalized = network_settings(settings)
        declarations = validate_model(normalized, model)
        assert bool(declarations) is (mode == "selected")
        if declarations:
            assert declarations == [{"service": "app", "target": 8080, "published": 18080}]


@pytest.mark.parametrize("field,value", [("URL", "127.0.0.1"), ("TargetPort", 18080), ("PublishedPort", 8080),
                                        ("Protocol", "udp")])
def test_loopback_or_wrong_published_bindings_cannot_pass_as_pod_reachability(field, value):
    status = publishers()
    status["services"][0]["Publishers"][0][field] = value
    with pytest.raises(acceptance.AcceptanceError, match="bindings"):
        acceptance.publisher_proof(status)


def test_publishers_require_both_distinct_healthy_services():
    assert acceptance.publisher_proof(publishers())["app"] == {"target": 8080, "published": 18080, "host_ip": "0.0.0.0"}
    for status in ({"services": publishers()["services"][:1]},
                   {"services": publishers()["services"] + publishers()["services"][:1]}):
        with pytest.raises(acceptance.AcceptanceError):
            acceptance.publisher_proof(status)
    status = publishers()
    status["services"][0]["Health"] = "unhealthy"
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.publisher_proof(status)


def test_dns_domain_comes_from_real_search_suffix_without_hardcoded_cluster_local():
    assert acceptance.domain_from_search(["approved-a.svc.internal.example", "svc.internal.example", "internal.example"], "approved-a") == "internal.example"


@pytest.mark.parametrize("search", [[], ["localhost"], ["svc.a.example", "svc.b.example"], [None], "svc.example"])
def test_missing_ambiguous_or_malformed_dns_search_requires_explicit_domain(search):
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.domain_from_search(search, "approved-a")


def test_engine_dns_programs_compile_and_resolve_only_captured_ready_pod(args, monkeypatch):
    runner = acceptance.Runner(args)
    ident = "012345abcdef"
    target = {"identity": ident, "namespace": args.namespace_b, "publishers": acceptance.publisher_proof(publishers()),
              "captured": {f"StatefulSet/pg-{ident}": "controller", f"Pod/pg-{ident}-0": "pod"}}
    pod = {"kind": "Pod", "metadata": {"uid": "pod", "ownerReferences": [{"uid": "controller", "kind": "StatefulSet", "controller": True}]},
           "status": {"podIP": "10.0.0.17", "phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
    def execute(role, program, **kwargs):
        compile(program, "actual-probe-program", "exec")
        return ({"search": ["approved-a.svc.internal.example"]} if "resolv.conf" in program else {"addresses": ["10.0.0.17"]}), 0
    monkeypatch.setattr(runner, "exec_json", execute)
    endpoint = runner.endpoint({"namespace": args.namespace_a}, target, {"items": [pod]})
    assert endpoint["dns"] == "pg-012345abcdef-0.pg-012345abcdef.approved-b.svc.internal.example"
    for mutation in ("uid", "owner", "ready", "deleting"):
        changed = deepcopy(pod)
        if mutation == "uid":
            changed["metadata"]["uid"] = "replacement"
        elif mutation == "owner":
            changed["metadata"]["ownerReferences"][0]["uid"] = "foreign-controller"
        elif mutation == "deleting":
            changed["metadata"]["deletionTimestamp"] = "now"
        else:
            changed["status"]["conditions"][0]["status"] = "False"
        with pytest.raises(acceptance.AcceptanceError):
            runner.endpoint({"namespace": args.namespace_a}, target, {"items": [changed]})


def test_real_http_probe_checks_exact_bytes_and_wrong_response_is_not_policy_pending():
    server = acceptance.base.LoopbackServer(b"nonce\x00\xff", "")
    try:
        for body, passed in ((b"nonce\x00\xff", True), (b"wrong", False)):
            result = subprocess.run([sys.executable, "-I", "-B", "-c", acceptance.probe_program("127.0.0.1", server.port, body)],
                                    capture_output=True, timeout=5)
            value = json.loads(result.stdout)
            assert (value["matched"] is True) is passed
            if not passed:
                assert value["reason"] == "AssertionError"
    finally:
        server.close()


def test_refused_connection_is_never_a_successful_isolation_probe():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = subprocess.run([sys.executable, "-I", "-B", "-c", acceptance.probe_program("127.0.0.1", port, None)],
                            capture_output=True, timeout=5)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"blocked": False, "reason": "ConnectionRefusedError"}


def test_cleanup_never_started_role_makes_no_cluster_or_cli_calls(args, monkeypatch):
    scoped = SimpleNamespace(**{**vars(args), "namespace": args.namespace_a})
    runner = acceptance.NamespaceRunner(scoped)
    command = Mock(side_effect=AssertionError("No external work for an unstarted fixture"))
    monkeypatch.setattr(runner, "command", command)
    role = {"role": "future-target", "attempted": False}
    runner.cleanup_one(role)
    runner.cleanup_one(role)
    command.assert_not_called()
    assert runner.result["cleanup"]["future-target"] == {"attempted": False, "passed": True, "not_started": True}


def test_cleanup_one_never_runs_for_other_roles_and_is_not_retried(args, monkeypatch):
    scoped = SimpleNamespace(**{**vars(args), "namespace": args.namespace_a})
    runner = acceptance.NamespaceRunner(scoped)
    one, two = {"role": "one", "attempted": True}, {"role": "two", "attempted": True}
    runner.fixtures = [one, two]
    runner.base = Path("/private/owned-fixtures")
    seen = []
    def cleanup(actual):
        seen.extend(actual.fixtures)
        assert actual.base is None
        actual.result["cleanup"]["one"] = {"passed": False, "error": "uncertain down"}
    monkeypatch.setattr(acceptance.base.Runner, "cleanup", cleanup)
    runner.cleanup_one(one)
    runner.cleanup_one(one)
    assert seen == [one] and runner.fixtures == [one, two] and runner.base == Path("/private/owned-fixtures")


@pytest.mark.parametrize("cleanup_ok", [True, False])
def test_cross_namespace_target_starts_only_after_same_namespace_cleanup_proof(args, monkeypatch, cleanup_ok):
    runner = acceptance.Runner(args)
    runner.roles = [{"role": name} for name in ("source", "same", "cross")]
    events = []
    scoped = SimpleNamespace(result={"cleanup": {}})
    def cleanup(role):
        events.append("down-same")
        scoped.result["cleanup"][role["role"]] = {"passed": cleanup_ok}
    scoped.cleanup_one = cleanup
    runner.runners = {"a": scoped}
    monkeypatch.setattr(runner, "phase", lambda name, source, target: events.append(name))
    if cleanup_ok:
        runner.exercise()
        assert events == ["same-namespace", "down-same", "cross-namespace"]
    else:
        with pytest.raises(acceptance.AcceptanceError, match="before"):
            runner.exercise()
        assert events == ["same-namespace", "down-same"]


def test_independent_two_engine_guard_prevents_any_accidental_third_start(args):
    runner = acceptance.Runner(args)
    runner.roles = [{"attempted": True}, {"attempted": True}, {"attempted": False}]
    with pytest.raises(acceptance.AcceptanceError, match="third engine"):
        runner.start(runner.roles[-1])


def test_prepare_keeps_state_private_and_each_runner_namespace_fixed(args, monkeypatch):
    runner = acceptance.Runner(args)
    commands = []
    def command(scoped, argv, **kwargs):
        commands.append(argv)
        if argv == [str(args.podgrove_bin), "--version"]:
            return "podgrove test-version", 0
        assert argv[:5] == ["kubectl", "--context", args.context, "--namespace", scoped.args.namespace]
        assert argv[-4:] == ["configmap", acceptance.MARKER, "-o", "json"]
        return json.dumps(marker(scoped.args.namespace)), 0
    monkeypatch.setattr(acceptance.NamespaceRunner, "command", command)
    monkeypatch.setattr(acceptance.NamespaceRunner, "cli", Mock(return_value=("{}", 0)))
    monkeypatch.setattr(acceptance.NamespaceRunner, "inventory", lambda self, role: ({"items": []}, {}))
    try:
        runner.prepare()
        assert len(runner.roles) == 3
        assert [role["namespace"] for role in runner.roles] == [args.namespace_a, args.namespace_a, args.namespace_b]
        assert runner.roles[1]["root"].name == runner.roles[2]["root"].name
        assert runner.roles[1]["identity"] != runner.roles[2]["identity"]
        for role in runner.roles:
            assert role["state"].is_relative_to(args.output)
            assert role["state"].stat().st_mode & 0o777 == 0o700
            assert role["config"]["forward"] == []
            assert role["config"]["resources"]["limits"] == {"cpu": "1", "memory": "1Gi"}
            assert role["config"]["storage"] == {"size": "2Gi"}
            assert len(role["root"].name) <= 63
        assert len(commands) == 3
    finally:
        runner.cleanup()
    assert runner.result["bootstrap_before"] == runner.result["bootstrap_after"]
    assert runner.result["fixture_directories_removed"]


def test_policy_propagation_retry_is_bounded_and_uses_new_probes(args, monkeypatch):
    runner = acceptance.Runner(args)
    runner.body = b"fixture"
    clock = [10.0]
    monkeypatch.setattr(acceptance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(acceptance.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    outcomes = iter([({"blocked": False, "reason": "connected"}, 1),
                     ({"blocked": True, "reason": "TCP timeout"}, 0)])
    execute = Mock(side_effect=lambda *args, **kwargs: next(outcomes))
    monkeypatch.setattr(runner, "exec_json", execute)
    runner.probe("converges", {}, "10.0.0.1", 18080, allowed=False)
    assert execute.call_count == 2 and len(runner.result["checks"]["converges"]["attempts"]) == 2
    execute.side_effect = lambda *args, **kwargs: ({"blocked": False, "reason": "connected"}, 1)
    with pytest.raises(acceptance.AcceptanceError, match="fixed packet-probe budget"):
        runner.probe("never-denied", {}, "10.0.0.1", 18080, allowed=False)
    assert clock[0] == 16


def test_probe_rejects_cli_failure_without_retries(args, monkeypatch):
    runner = acceptance.Runner(args)
    scoped = SimpleNamespace(cli=Mock(return_value=("Unauthorized", 1)))
    runner.runners = {"a": scoped}
    with pytest.raises(acceptance.AcceptanceError, match="authentication"):
        runner.probe("deny", {"runner": "a"}, "127.0.0.1", 18080, allowed=False)
    assert scoped.cli.call_count == 1


def test_packet_timeout_is_the_only_accepted_negative(args, monkeypatch):
    runner = acceptance.Runner(args)
    execute = Mock(return_value=({"blocked": True, "reason": "TCP timeout"}, 0))
    monkeypatch.setattr(runner, "exec_json", execute)
    runner.probe("deny", {}, "10.0.0.1", 18080, allowed=False)
    assert runner.result["checks"]["deny"]["expected"] == "blocked"
    execute.return_value = ({"blocked": False, "reason": "ConnectionRefusedError"}, 2)
    with pytest.raises(acceptance.AcceptanceError, match="non-policy"):
        runner.probe("not-deny", {}, "10.0.0.1", 18080, allowed=False)


def test_execution_and_namespace_gates_precede_runner_creation(args, monkeypatch):
    runner = Mock()
    monkeypatch.setattr(acceptance, "Runner", runner)
    cli = ["--podgrove-bin", str(args.podgrove_bin), "--context", args.context,
           "--namespace-a", args.namespace_a, "--namespace-b", args.namespace_b,
           "--storage-class", args.storage_class, "--output", str(args.output)]
    with pytest.raises(SystemExit):
        acceptance.main(cli)
    duplicate = deepcopy(cli)
    duplicate[duplicate.index("--namespace-b") + 1] = args.namespace_a
    with pytest.raises(SystemExit):
        acceptance.main([*duplicate, "--execute"])
    runner.assert_not_called()


def test_existing_output_is_never_overwritten(args):
    args.output.mkdir()
    (args.output / "result.json").write_text("existing")
    assert acceptance.Runner(args).run() == 1
    assert (args.output / "result.json").read_text() == "existing"


@pytest.mark.parametrize('message', ['Unauthorized', 'the server has asked for the client to provide credentials',
                                    'You must be logged in to the server', 'ExpiredToken: expired'])
def test_authentication_failure_latches_across_namespaces_before_any_more_calls(args, message, monkeypatch):
    auth_stop = {}
    first = acceptance.NamespaceRunner(SimpleNamespace(**{**vars(args), 'namespace': args.namespace_a}), auth_stop)
    second = acceptance.NamespaceRunner(SimpleNamespace(**{**vars(args), 'namespace': args.namespace_b}), auth_stop)
    args.output.mkdir()
    first.base = args.output
    with pytest.raises(acceptance.AcceptanceError, match='Authentication failed'):
        first.command([sys.executable, '-c', 'import sys;sys.stderr.write(' + repr(message) + ');sys.exit(1)'], check=False)
    assert auth_stop['namespace'] == args.namespace_a
    assert 'evidence' in auth_stop and message not in auth_stop.values()
    popen = Mock(side_effect=AssertionError('No subprocess after an authentication failure'))
    monkeypatch.setattr(acceptance.base.subprocess, 'Popen', popen)
    with pytest.raises(acceptance.AcceptanceError, match='all further external calls'):
        second.command(['kubectl', 'get', 'pod'])
    role = {'role': 'target', 'attempted': True, 'identity': '012345abcdef', 'namespace': args.namespace_b}
    second.cleanup_one(role)
    assert second.result['cleanup']['target']['unconfirmed']
    popen.assert_not_called()


def test_authentication_phrase_split_across_evidence_chunks_is_detected(args):
    args.output.mkdir()
    runner = acceptance.NamespaceRunner(SimpleNamespace(**{**vars(args), 'namespace': args.namespace_a}))
    runner.base = args.output
    program = "import sys;sys.stderr.write('x'*65532+'Unauthorized');sys.exit(1)"
    with pytest.raises(acceptance.AcceptanceError, match='Authentication failed'):
        runner.command([sys.executable, '-c', program])
    assert runner.auth_stop


def test_same_namespace_only_never_reads_or_starts_second_namespace(args, monkeypatch):
    args.same_namespace_only, args.namespace_b = True, None
    runner = acceptance.Runner(args)
    commands = []
    def command(scoped, argv, **kwargs):
        commands.append(argv)
        if argv[-1] == '--version':
            return 'podgrove test-version', 0
        assert scoped.args.namespace == args.namespace_a
        return json.dumps(marker(args.namespace_a)), 0
    monkeypatch.setattr(acceptance.NamespaceRunner, 'command', command)
    monkeypatch.setattr(acceptance.NamespaceRunner, 'cli', Mock(return_value=('{}', 0)))
    monkeypatch.setattr(acceptance.NamespaceRunner, 'inventory', lambda self, role: ({'items': []}, {}))
    phases = []
    monkeypatch.setattr(runner, 'phase', lambda name, *args: phases.append(name))
    assert runner.run() == 0
    assert phases == ['same-namespace']
    assert set(runner.runners) == {'a'} and len(runner.roles) == 2
    assert runner.result['namespaces'] == [args.namespace_a]
    assert runner.result['cleanup']['cross-namespace-target']['not_started']
    assert runner.result['not_run'] == ['cross-namespace matrix', 'wrong-namespace selection checks']
    assert runner.result['fixture_directories_removed']
    assert len(commands) == 3


def test_same_namespace_flag_is_required_when_namespace_b_is_omitted(args, monkeypatch):
    runner = Mock()
    runner.return_value.run.return_value = 0
    monkeypatch.setattr(acceptance, 'Runner', runner)
    cli = ['--execute', '--podgrove-bin', str(args.podgrove_bin), '--context', args.context,
           '--namespace-a', args.namespace_a, '--storage-class', args.storage_class, '--output', str(args.output)]
    with pytest.raises(SystemExit):
        acceptance.main(cli)
    runner.assert_not_called()
    assert acceptance.main([*cli, '--same-namespace-only']) == 0
    assert runner.call_args.args[0].namespace_b is None


def test_authentication_failure_records_remaining_scope_without_cleanup_or_bootstrap_calls(args, monkeypatch):
    runner = acceptance.Runner(args)
    args.output.mkdir()
    runner.created = True
    role = {'role': 'source', 'attempted': True, 'identity': '012345abcdef', 'namespace': args.namespace_a,
            'root': args.output / 'worktree', 'state': args.output / 'state', 'runner': 'a'}
    runner.roles = [role]
    scoped = acceptance.NamespaceRunner(SimpleNamespace(**{**vars(args), 'namespace': args.namespace_a}), runner.auth_stop)
    runner.runners = {'a': scoped}
    runner.result['bootstrap_before'] = {'a': marker(args.namespace_a)}
    def prepare():
        runner.auth_stop.update(reason='Authentication unavailable', namespace=args.namespace_a)
        raise acceptance.AcceptanceError('Authentication failed')
    monkeypatch.setattr(runner, 'prepare', prepare)
    bootstrap = Mock(side_effect=AssertionError('No readback after auth gate'))
    monkeypatch.setattr(runner, 'bootstrap', bootstrap)
    assert runner.run() == 1
    bootstrap.assert_not_called()
    assert runner.result['remaining_unconfirmed'] == [{key: str(role[key]) for key in ('identity', 'namespace', 'state', 'root')}]
    assert runner.result['authentication_stop']['namespace'] == args.namespace_a
    assert runner.result['cleanup']['source']['unconfirmed']
