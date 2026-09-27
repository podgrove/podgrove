"""Release safety checks without publishing, Homebrew installation, or a cluster."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tarfile
import tomllib
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


formula = load_script("homebrew_formula")
verify = load_script("verify_release")
resolver = load_script("resolve_release")
publisher = load_script("publish_release")


@pytest.fixture
def lock():
    def dependency(name, **extra):
        return {"name": name, "version": "1.0.0", "source": {"registry": "https://pypi.org/simple"},
                "sdist": {"url": f"https://files.pythonhosted.org/packages/ab/{name}-1.0.0.tar.gz",
                          "hash": "sha256:" + "a" * 64}, **extra}
    return {"package": [
        {"name": "podgrove", "version": "0.1.0", "source": {"editable": "."},
         "dependencies": [{"name": "direct"}], "optional-dependencies": {"test": [{"name": "pytest"}]}},
        dependency("direct", dependencies=[{"name": "transitive", "marker": "python_full_version < '3.13'"}]),
        dependency("transitive"), dependency("pytest"),
    ]}


def render(lock, **kwargs):
    return formula.render(version="0.1.0", sdist_sha256="b" * 64, lock=lock, license_id="MIT", **kwargs)


def test_runtime_closure_includes_transitive_backport_excludes_test_extras(lock):
    assert [item["name"] for item in formula.runtime_packages(lock)] == ["direct", "transitive"]
    result = render(lock)
    assert 'resource "transitive"' in result
    assert "pytest" not in result
    assert 'virtualenv_install_with_resources' in result
    assert '/releases/download/v0.1.0/podgrove-0.1.0.tar.gz' in result
    assert result.count('sha256 "') == 3


def test_generation_deterministic_under_lock_order_changes(lock):
    previous = render(lock)
    lock["package"].reverse()
    assert render(lock) == previous


@pytest.mark.parametrize("bad", ["v1.0.0", "1.0", "1.0.0-rc.1", "1.0.0;whoami", "1.0.0\n", "01.0.0", "../tag"])
def test_version_input_rejected(bad):
    with pytest.raises(ValueError):
        formula.validated_version(bad)


@pytest.mark.parametrize("change", ["duplicate", "missing", "private", "fork", "root"])
def test_unknown_lock_graph_is_refused(lock, change):
    if change == "duplicate":
        lock["package"].append(deepcopy(lock["package"][1]))
    elif change == "missing":
        lock["package"].pop(2)
    elif change == "private":
        lock["package"][2]["source"] = {"registry": "https://packages.private.test/simple"}
    elif change == "fork":
        lock["package"][0]["dependencies"][0]["version"] = "2.0.0"
    else:
        lock["package"][0]["source"] = {"virtual": "."}
    with pytest.raises(ValueError):
        render(lock)


@pytest.mark.parametrize("url", ["http://files.pythonhosted.org/packages/a.tar.gz", "https://evil.test/a.tar.gz",
                                  'https://files.pythonhosted.org/packages/a#{`command`}.tar.gz',
                                  "https://files.pythonhosted.org/packages/a.tar.gz?q=1",
                                  "https://user@files.pythonhosted.org/packages/a.tar.gz"])
def test_untrusted_dependency_url_is_refused(lock, url):
    lock["package"][1]["sdist"]["url"] = url
    with pytest.raises(ValueError):
        render(lock)


@pytest.mark.parametrize("field,value", [("hash", "sha256:bad"), ("url", "")])
def test_missing_dependency_source_is_refused(lock, field, value):
    lock["package"][1]["sdist"][field] = value
    with pytest.raises(ValueError):
        render(lock)


def test_stale_version_and_license_refused(lock):
    lock["package"][0]["version"] = "0.2.0"
    with pytest.raises(ValueError, match="version"):
        render(lock)
    for value in [None, {}, 'MIT"; system("whoami")']:
        with pytest.raises(ValueError, match="license"):
            formula.render(version="0.1.0", sdist_sha256="a" * 64, lock=lock, license_id=value)


def test_repository_input_cannot_inject_ruby(lock):
    with pytest.raises(ValueError):
        render(lock, repository='x/y";system("whoami")')


def test_real_lock_closure_has_all_seven_runtime_sources():
    data = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = formula.runtime_packages(data)
    assert {item["name"] for item in packages} == {
        "attrs", "jsonschema", "jsonschema-specifications", "pyyaml", "referencing", "rpds-py", "typing-extensions"}
    for package in packages:
        formula.source_resource(package)
    value = formula.render(version=verify.project_version(ROOT), lock=data, license_id="MIT", sdist_sha256="b" * 64)
    assert 'depends_on "rust" => :build' in value


def make_source(tmp_path):
    root = tmp_path / "source"
    (root / "podgrove/web_static").mkdir(parents=True)
    (root / "podgrove/__init__.py").write_text('__version__ = "0.1.0"\n')
    (root / "podgrove/web_static/index.html").write_text("<html>dashboard</html>")
    (root / "pyproject.toml").write_text('[project]\nname="podgrove"\nversion="0.1.0"\n')
    (root / "uv.lock").write_text('[[package]]\nname="podgrove"\nversion="0.1.0"\n')
    (root / "README.md").write_text("Public documentation\n")
    (root / "LICENSE").write_text("MIT test license\n")
    (root / ".release-please-manifest.json").write_text('{".": "0.1.0"}\n')
    return root


def make_archives(root, *, extra_source=None, bad_wheel=False):
    dist = root.parent / "dist"
    dist.mkdir()
    sdist = dist / "podgrove-0.1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.add(path, arcname="podgrove-0.1.0/" + path.relative_to(root).as_posix())
        if extra_source:
            info = tarfile.TarInfo(extra_source)
            info.size = 3
            archive.addfile(info, io.BytesIO(b"bad"))
    wheel = dist / "podgrove-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path in (root / "podgrove").rglob("*"):
            if path.is_file():
                archive.writestr(path.relative_to(root).as_posix(), b"changed" if bad_wheel else path.read_bytes())
        archive.writestr("podgrove-0.1.0.dist-info/METADATA", "Name: podgrove\nVersion: 0.1.0\n")
        archive.writestr("podgrove-0.1.0.dist-info/licenses/LICENSE", "MIT test license\n")
    return dist


def test_archive_parity_and_version_agreement(tmp_path):
    root = make_source(tmp_path)
    dist = make_archives(root)
    assert verify.project_version(root) == "0.1.0"
    verify.check_archives(root, dist, "0.1.0")
    (root / "podgrove/__init__.py").write_text('__version__="0.2.0"')
    with pytest.raises(ValueError, match="agree"):
        verify.project_version(root)


@pytest.mark.parametrize("member", ["podgrove-0.1.0/podgrove.yml", "podgrove-0.1.0/.env",
                                    "podgrove-0.1.0/HANDOFF.md", "podgrove-0.1.0/artifacts/private.json", "../escape"])
def test_private_or_unsafe_sdist_refused(tmp_path, member):
    root = make_source(tmp_path)
    dist = make_archives(root, extra_source=member)
    with pytest.raises(ValueError):
        verify.check_archives(root, dist, "0.1.0")


def test_altered_wheel_refused(tmp_path):
    root = make_source(tmp_path)
    dist = make_archives(root, bad_wheel=True)
    with pytest.raises(ValueError, match="differs"):
        verify.check_archives(root, dist, "0.1.0")


def test_stale_dist_files_refused(tmp_path):
    root = make_source(tmp_path)
    dist = make_archives(root)
    (dist / "old.whl").write_text("old build")
    with pytest.raises(ValueError, match="exactly"):
        verify.check_archives(root, dist, "0.1.0")


def test_install_requirements_pin_wheel_and_runtime_hashes(tmp_path, lock):
    wheel = tmp_path / "podgrove.whl"
    wheel.write_bytes(b"wheel fixture")
    requirements = verify.locked_requirements(lock, wheel)
    assert "--hash=sha256:" + verify.sha256(wheel) in requirements
    assert "direct==1.0.0" in requirements and "transitive==1.0.0" in requirements
    assert not any(line.startswith("pytest==") for line in requirements.splitlines())
    lock["package"][1]["sdist"]["hash"] = "invalid"
    with pytest.raises(ValueError, match="hashes"):
        verify.locked_requirements(lock, wheel)


@pytest.fixture
def bundle(tmp_path):
    dist = tmp_path / "bundle"
    dist.mkdir()
    for name in ["podgrove-0.1.0.tar.gz", "podgrove-0.1.0-py3-none-any.whl", "podgrove.rb"]:
        (dist / name).write_text(name)
    manifest = {"version": "0.1.0", "tag": "v0.1.0", "source_commit": "c" * 40,
                "assets": {path.name: verify.sha256(path) for path in dist.iterdir()}, "checks": {}}
    (dist / "release-manifest.json").write_text(json.dumps(manifest))
    (dist / "SHA256SUMS").write_text("".join(f"{verify.sha256(path)}  {path.name}\n" for path in sorted(dist.iterdir())))
    return dist


def test_bundle_tampering_refused(bundle):
    verify.verify_bundle(bundle)
    (bundle / "podgrove.rb").write_text("tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify.verify_bundle(bundle)


def test_checksum_or_extra_asset_refused(bundle):
    (bundle / "extra.txt").write_text("unreviewed")
    with pytest.raises(ValueError, match="Unexpected file"):
        verify.verify_bundle(bundle)
    (bundle / "extra.txt").unlink()
    (bundle / "SHA256SUMS").write_text("wrong")
    with pytest.raises(ValueError, match="Checksum"):
        verify.verify_bundle(bundle)


@pytest.fixture
def github(bundle, monkeypatch):
    data = {"draft": True, "tag_name": "v0.1.0", "assets": []}
    uploaded = {}
    commands = []
    monkeypatch.setattr(publisher, "resolve", lambda *_: (deepcopy(data), "c" * 40))

    def digest(repository, asset):
        return hashlib.sha256(uploaded[asset["name"]]).hexdigest()

    def run(command, **kwargs):
        commands.append(command)
        assert "--clobber" not in command
        if command[2] == "upload":
            path = Path(command[4])
            assert path.name not in uploaded
            uploaded[path.name] = path.read_bytes()
            data["assets"].append({"id": len(uploaded), "name": path.name})
        elif command[2] == "edit":
            data["draft"] = False
        else:
            raise AssertionError(command)
    monkeypatch.setattr(publisher, "download_digest", digest)
    monkeypatch.setattr(publisher.subprocess, "run", run)
    return data, uploaded, commands


def test_publish_verifies_bytes_before_publication_and_retry_is_read_only(bundle, github):
    data, uploaded, commands = github
    publisher.publish("podgrove/podgrove", bundle, expected_tag="v0.1.0", expected_sha="c" * 40)
    assert len(uploaded) == 5
    assert commands[-1][2] == "edit"
    assert data["draft"] is False
    commands.clear()
    publisher.publish("podgrove/podgrove", bundle, expected_tag="v0.1.0", expected_sha="c" * 40)
    assert commands == []


def test_partial_upload_retry_keeps_exact_existing_bytes(bundle, github):
    data, uploaded, commands = github
    uploaded["podgrove.rb"] = (bundle / "podgrove.rb").read_bytes()
    data["assets"].append({"id": 1, "name": "podgrove.rb"})
    publisher.publish("podgrove/podgrove", bundle, expected_tag="v0.1.0", expected_sha="c" * 40)
    assert sum(command[2] == "upload" for command in commands) == 4


def test_publish_never_replaces_conflicting_asset(bundle, github):
    data, uploaded, commands = github
    uploaded["SHA256SUMS"] = b"other version bytes"
    data["assets"].append({"id": 1, "name": "SHA256SUMS"})
    with pytest.raises(ValueError, match="refusing replacement"):
        publisher.publish("podgrove/podgrove", bundle, expected_tag="v0.1.0", expected_sha="c" * 40)
    assert commands == []


def test_published_incomplete_release_is_not_mutated(bundle, github):
    data, _, commands = github
    data["draft"] = False
    with pytest.raises(ValueError, match="Published release is missing"):
        publisher.publish("podgrove/podgrove", bundle, expected_tag="v0.1.0", expected_sha="c" * 40)
    assert commands == []


def test_tag_move_prevents_publication(bundle, github, monkeypatch):
    monkeypatch.setattr(publisher, "resolve", lambda *_: ({"draft": True, "assets": []}, "d" * 40))
    with pytest.raises(ValueError, match="source disagree"):
        publisher.publish("podgrove/podgrove", bundle, expected_tag="v0.1.0", expected_sha="c" * 40)
    assert github[2] == []


@pytest.mark.parametrize("tag", ["--help", "../tag", "v1.0.0;echo unsafe", "v1.0.0\nmalicious", "v1.0.0-rc1"])
def test_resolve_rejects_unsafe_tags_before_gh(monkeypatch, tag):
    monkeypatch.setattr(resolver, "gh_json", lambda *_: pytest.fail("Invalid input reached GitHub"))
    with pytest.raises(ValueError):
        resolver.resolve("podgrove/podgrove", tag)


def test_workflow_permissions_pins_and_publication_guards():
    workflows = {}
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        data = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        workflows[path.name] = data
        assert data["permissions"]["contents"] == "read"
        for job in data["jobs"].values():
            for step in job.get("steps", []):
                if uses := step.get("uses"):
                    assert re.fullmatch(r"[^@]+@[a-f0-9]{40}", uses), uses
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] == "false"
                assert "${{" not in step.get("run", ""), "Pass dynamic values through env/structured action inputs"
    release = workflows["release.yml"]
    assert "release" not in release["on"], "Do not rely on GITHUB_TOKEN release events triggering workflows"
    assert "RELEASE_AUTOMATION_ENABLED" in release["jobs"]["release-please"]["if"]
    assert "RELEASE_AUTOMATION_ENABLED" in release["jobs"]["resolve"]["if"]
    assert release["on"]["workflow_dispatch"]["inputs"]["artifact_run_id"]["required"] == "true"
    assert release["jobs"]["publish"]["environment"] == "release"
    assert release["jobs"]["homebrew-pr"]["environment"] == "homebrew"
    assert "HOMEBREW_TAP_REPOSITORY" in release["jobs"]["homebrew-pr"]["if"]
    config = json.loads((ROOT / "release-please-config.json").read_text())
    assert config["draft"] is True and config["force-tag-creation"] is True
    assert config["packages"]["."]["extra-files"][0]["jsonpath"] == "$.package[?(@.name=='podgrove')].version"


def test_other_release_artifact_cannot_be_published(bundle, github):
    with pytest.raises(ValueError, match="requested release"):
        publisher.publish("podgrove/podgrove", bundle, expected_tag="v0.2.0", expected_sha="c" * 40)
    assert github[2] == []


def test_extra_package_source_refused(tmp_path):
    root = make_source(tmp_path)
    dist = make_archives(root, extra_source="podgrove-0.1.0/podgrove/injected.py")
    with pytest.raises(ValueError, match="payload differs"):
        verify.check_archives(root, dist, "0.1.0")


@pytest.mark.parametrize("name", ["podgrove/injected.py", "podgrove-0.1.0.dist-info/../../escape.py"])
def test_extra_or_unsafe_wheel_source_refused(tmp_path, name):
    root = make_source(tmp_path)
    dist = make_archives(root)
    with zipfile.ZipFile(dist / "podgrove-0.1.0-py3-none-any.whl", "a") as archive:
        archive.writestr(name, "injected")
    with pytest.raises(ValueError):
        verify.check_archives(root, dist, "0.1.0")


def test_uv_build_marker_removed_without_accepting_unknown_content(tmp_path):
    marker = tmp_path / ".gitignore"
    marker.write_bytes(b"*")
    verify.remove_build_marker(tmp_path)
    assert not marker.exists()
    marker.write_text("unexpected")
    with pytest.raises(ValueError):
        verify.remove_build_marker(tmp_path)
    assert marker.read_text() == "unexpected"


@pytest.fixture
def release_api(monkeypatch):
    replies, calls = {}, []
    def run(command, **kwargs):
        assert command[:2] == ["gh", "api"] and command[-1] == "--include"
        assert kwargs == {"check": False, "capture_output": True, "text": True, "timeout": 60}
        endpoint = command[2]
        calls.append(endpoint)
        status, body = replies[endpoint]
        return resolver.subprocess.CompletedProcess(command, 0 if status == 200 else 1,
            f"HTTP/2.0 {status} Fixture\r\nContent-Type: application/json\r\n\r\n{json.dumps(body)}",
            "" if status == 200 else f"gh: fixture (HTTP {status})")
    monkeypatch.setattr(resolver.subprocess, "run", run)
    return replies, calls


def draft_lookup(replies):
    base = "repos/podgrove/podgrove"
    release = {"id": 123, "tag_name": "v0.2.0", "draft": True, "prerelease": False, "assets": []}
    replies[f"{base}/releases/tags/v0.2.0"] = 404, {"message": "Not Found"}
    replies[f"{base}/releases?per_page=100&page=1"] = 200, [release]
    replies[f"{base}/releases/123"] = 200, release
    replies[f"{base}/commits/v0.2.0"] = 200, {"sha": "e" * 40}
    return base, release


def test_resolver_published_tag_uses_direct_endpoint_without_listing(release_api):
    replies, calls = release_api
    base, release = draft_lookup(replies)
    release["draft"] = False
    replies[f"{base}/releases/tags/v0.2.0"] = 200, release
    assert resolver.resolve("podgrove/podgrove", "v0.2.0") == (release, "e" * 40)
    assert calls == [f"{base}/releases/tags/v0.2.0", f"{base}/commits/v0.2.0"]


def test_resolver_draft_404_falls_back_to_paginated_exact_tag_then_id(release_api):
    replies, calls = release_api
    base, release = draft_lookup(replies)
    # A prefix match, API-provided foreign URL and other pages cannot redirect
    # resolution. Only the exact tag's constructed endpoint is read.
    release["url"] = "https://attacker.invalid/releases/123"
    first = [{"id": 1000 + index, "tag_name": f"v0.2.{index + 1}"} for index in range(100)]
    replies[f"{base}/releases?per_page=100&page=1"] = 200, first
    replies[f"{base}/releases?per_page=100&page=2"] = 200, [release]
    assert resolver.resolve("podgrove/podgrove", "v0.2.0") == (release, "e" * 40)
    assert calls == [f"{base}/releases/tags/v0.2.0", f"{base}/releases?per_page=100&page=1",
                     f"{base}/releases?per_page=100&page=2", f"{base}/releases/123", f"{base}/commits/v0.2.0"]


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_resolver_non_404_never_falls_back_even_if_body_mentions_404(release_api, status):
    replies, calls = release_api
    endpoint = "repos/podgrove/podgrove/releases/tags/v0.2.0"
    replies[endpoint] = status, {"message": "Not Found (HTTP 404)"}
    with pytest.raises(resolver.GitHubAPIError) as caught:
        resolver.resolve("podgrove/podgrove", "v0.2.0")
    assert caught.value.status == status and calls == [endpoint]


def test_resolver_transport_failure_with_404_text_is_not_http_404(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return resolver.subprocess.CompletedProcess(command, 1, "", "proxy connection failed (HTTP 404)")
    monkeypatch.setattr(resolver.subprocess, "run", run)
    with pytest.raises(resolver.GitHubAPIError) as caught:
        resolver.resolve("podgrove/podgrove", "v0.2.0")
    assert caught.value.status is None and len(calls) == 1


@pytest.mark.parametrize("entries,message", [([], "not found"),
    ([{"id": 1, "tag_name": "v0.2.01"}], "not found"),
    ([{"id": 1, "tag_name": "v0.2.0"}, {"id": 2, "tag_name": "v0.2.0"}], "ambiguous"),
    ({"message": "not a page"}, "malformed"),
    ([{"id": True, "tag_name": "v0.2.0"}], "malformed"),
    ([{"id": 1, "tag_name": None}], "malformed"),
])
def test_resolver_missing_ambiguous_or_malformed_fallback_refuses_commit_reads(release_api, entries, message):
    replies, calls = release_api
    base, _ = draft_lookup(replies)
    replies[f"{base}/releases?per_page=100&page=1"] = 200, entries
    with pytest.raises(ValueError, match=message):
        resolver.resolve("podgrove/podgrove", "v0.2.0")
    assert len(calls) == 2


def test_resolver_duplicate_on_later_page_is_not_hidden_by_early_match(release_api):
    replies, calls = release_api
    base, release = draft_lookup(replies)
    first = [release, *({"id": 1000 + index, "tag_name": f"v1.0.{index}"} for index in range(99))]
    replies[f"{base}/releases?per_page=100&page=1"] = 200, first
    replies[f"{base}/releases?per_page=100&page=2"] = 200, [{**release, "id": 456}]
    with pytest.raises(ValueError, match="ambiguous"):
        resolver.resolve("podgrove/podgrove", "v0.2.0")
    assert len(calls) == 3


def test_resolver_incomplete_pagination_refuses_even_one_found_match(release_api, monkeypatch):
    replies, calls = release_api
    base, release = draft_lookup(replies)
    monkeypatch.setattr(resolver, "MAX_RELEASE_PAGES", 2)
    for page in (1, 2):
        rows = [{"id": page * 1000 + index, "tag_name": f"v{page}.0.{index}"} for index in range(100)]
        if page == 1:
            rows[0] = release
        replies[f"{base}/releases?per_page=100&page={page}"] = 200, rows
    with pytest.raises(ValueError, match="bounded lookup"):
        resolver.resolve("podgrove/podgrove", "v0.2.0")
    assert len(calls) == 3


def test_resolver_listing_auth_failure_is_not_treated_as_missing_release(release_api):
    replies, calls = release_api
    base, _ = draft_lookup(replies)
    replies[f"{base}/releases?per_page=100&page=1"] = 403, {"message": "Forbidden"}
    with pytest.raises(resolver.GitHubAPIError) as caught:
        resolver.resolve("podgrove/podgrove", "v0.2.0")
    assert caught.value.status == 403 and len(calls) == 2


@pytest.mark.parametrize("change,message", [({"id": 456}, "ID changed"),
                                            ({"tag_name": "v0.2.1"}, "requested stable"),
                                            ({"prerelease": True}, "requested stable")])
def test_resolver_fallback_reread_retains_tag_and_stable_release_checks(release_api, change, message):
    replies, calls = release_api
    base, release = draft_lookup(replies)
    replies[f"{base}/releases/123"] = 200, {**release, **change}
    with pytest.raises(ValueError, match=message):
        resolver.resolve("podgrove/podgrove", "v0.2.0")
    assert len(calls) == 3


def test_resolver_fallback_still_requires_real_commit_and_expected_sha(release_api, monkeypatch):
    replies, _ = release_api
    base, _ = draft_lookup(replies)
    replies[f"{base}/commits/v0.2.0"] = 200, {"sha": "not-a-commit"}
    with pytest.raises(ValueError, match="resolve to a commit"):
        resolver.resolve("podgrove/podgrove", "v0.2.0")
    replies[f"{base}/commits/v0.2.0"] = 200, {"sha": "e" * 40}
    monkeypatch.setattr(sys, "argv", ["resolve_release.py", "--repository", "podgrove/podgrove",
                                    "--tag", "v0.2.0", "--expected-sha", "f" * 40])
    with pytest.raises(SystemExit) as caught:
        resolver.main()
    assert caught.value.code == 2
