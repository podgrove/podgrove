import pytest

from podgrove.compose import Compose
from podgrove.config import load_config
from podgrove.errors import PodgroveError
from podgrove.sync_filter import excluded, validate_patterns


@pytest.mark.parametrize("path,pattern,expected", [
    ("src/__pycache__/a.pyc", "__pycache__", True), ("src/a.pyc", "*.pyc", True),
    ("src/a.py", "*.pyc", False), ("src/cache/a", "cache/", True),
    ("src/generated/a", "src/generated", True), ("other/src/generated/a", "src/generated", False),
    ("src/cache/a", "src/**/cache", True), ("src/x/y/cache/a", "src/**/cache", True),
    ("src/x/y/cache/a", "src/*/cache", False), ("nested/a.txt", "**/*.txt", True),
    ("a.txt", "**/*.txt", True), (".pytest_cache/x", ".pytest_cache", True),
])
def test_relative_patterns(path, pattern, expected):
    assert excluded(path, validate_patterns([pattern])) is expected


@pytest.mark.parametrize("pattern", ["/tmp", "../x", "a/../x", "!keep", "a\\b", "", "a//b", "./a", "a\n"])
def test_unsafe_ambiguous_patterns_rejected(pattern):
    with pytest.raises(PodgroveError, match="sync.exclude"):
        validate_patterns([pattern])


def test_excludes_prune_nested_symlinks_but_never_hide_explicit_mount(tmp_path):
    (tmp_path / "compose.yaml").write_text("services: {}")
    (tmp_path / "podgrove.yml").write_text("sync: {exclude: [__pycache__]}\n")
    source = tmp_path / "src"
    source.mkdir()
    (source / "__pycache__").symlink_to("/nonexistent")
    compose = Compose(load_config(tmp_path))
    compose._check_sync_path(str(source), "volumes")
    with pytest.raises(PodgroveError):
        compose._check_sync_path(str(source / "__pycache__"), "volumes")
    with pytest.raises(PodgroveError, match="symlinks"):
        compose._check_sync_path(str(source), "develop.watch", mirror=False)


def test_explicit_regular_bind_cannot_be_excluded(tmp_path):
    (tmp_path / "compose.yaml").write_text("services: {}")
    (tmp_path / "podgrove.yml").write_text("sync: {exclude: ['*.pyc']}\n")
    (tmp_path / "app.pyc").write_bytes(b"cache")
    with pytest.raises(PodgroveError, match="explicit sync source is excluded"):
        Compose(load_config(tmp_path))._check_sync_path(str(tmp_path / "app.pyc"), "volumes")
