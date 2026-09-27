"""Offline package proof preserves prior delivery and includes shipped sources."""
import json

import pytest

from scripts import verify_package as proof


def test_output_refuses_existing_evidence_and_symlink_parent(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    (old / "proof.json").write_text("keep")
    with pytest.raises(ValueError, match="new ordinary"):
        proof.prepare_output(old)
    linked = tmp_path / "linked"
    linked.symlink_to(old, target_is_directory=True)
    with pytest.raises(ValueError, match="new ordinary"):
        proof.prepare_output(linked / "new")
    assert list(old.iterdir()) == [old / "proof.json"]
    assert (old / "proof.json").read_text() == "keep"


def test_source_snapshot_includes_scripts_fixtures_modules_but_not_workspace_target(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "REPO", tmp_path)
    expected = ("README.md", "LICENSE", "pyproject.toml", "podgrove/repository.py",
                "podgrove/web_static/favicon.svg", "scripts/verify_docs.py",
                "tests/fixtures/example.env", "tests/fixtures/Dockerfile", "docs/configuration.md",
                "deploy/reaper/10-cronjob.yaml.example", "examples/shared/podgrove.yml", "schema/podgrove-v1.schema.json")
    ignored = ("podgrove.yml", "HANDOFF.md", "artifacts/private.txt", "podgrove/__pycache__/module.pyc", "tests/cache.bin")
    for name in (*expected, *ignored):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    manifest = tmp_path / "publication/public-files.json"
    manifest.parent.mkdir()
    raw = json.dumps({"version": 1, "files": sorted((*expected, "publication/public-files.json"))}).encode()
    manifest.write_bytes(raw)
    assert proof.source_snapshot() == {**{name: name.encode() for name in expected}, "publication/public-files.json": raw}


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "proof"
    output.mkdir()
    archives = output / "dist"
    archives.mkdir()
    target = repo / "dist"
    target.mkdir()
    names = ("podgrove-0.1.0-py3-none-any.whl", "podgrove-0.1.0.tar.gz")
    for name in names:
        (archives / name).write_bytes(b"verified " + name.encode())
        (target / name).write_bytes(b"prior " + name.encode())
    (target / "user-notes.txt").write_text("unrelated")
    monkeypatch.setattr(proof, "REPO", repo)
    monkeypatch.setattr(proof, "BASE", output, raising=False)
    monkeypatch.setattr(proof, "DIST", archives, raising=False)
    monkeypatch.setattr(proof, "source_snapshot", lambda: {"source": b"frozen"})
    monkeypatch.setattr(proof, "report", {
        "source_sha256": {"source": proof.digest(b"frozen")},
        "archives": {name: {"sha256": proof.digest((archives / name).read_bytes()),
                            "bytes": (archives / name).stat().st_size} for name in names},
    }, raising=False)
    return output, archives, target, names


def test_delivery_archives_matching_pair_and_retains_unrelated_files(delivery):
    output, archives, target, names = delivery
    result = proof.deliver()
    assert result["status"] == "passed"
    for name in names:
        assert (output / "prior-dist" / name).read_bytes() == b"prior " + name.encode()
        assert (target / name).read_bytes() == (archives / name).read_bytes()
    assert (target / "user-notes.txt").read_text() == "unrelated"
    assert set(path.name for path in target.iterdir()) == {*names, "user-notes.txt"}


def test_delivery_refuses_changed_sources_before_touching_prior_archives(delivery, monkeypatch):
    output, _archives, target, names = delivery
    monkeypatch.setattr(proof, "source_snapshot", lambda: {"source": b"edited"})
    with pytest.raises(AssertionError, match="Source changed"):
        proof.deliver()
    assert not (output / "prior-dist").exists()
    assert all((target / name).read_bytes() == b"prior " + name.encode() for name in names)


def test_delivery_refuses_prior_symlink_without_touching_its_target(delivery):
    output, _archives, target, names = delivery
    outside = output / "unrelated"
    outside.write_text("keep")
    (target / names[0]).unlink()
    (target / names[0]).symlink_to(outside)
    with pytest.raises(ValueError, match="non-regular"):
        proof.deliver()
    assert outside.read_text() == "keep"
    assert (target / names[1]).read_bytes() == b"prior " + names[1].encode()
