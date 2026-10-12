import pytest

import podgrove
from podgrove import cli


def test_cli_version_uses_installed_distribution(monkeypatch, capsys):
    monkeypatch.setattr(podgrove.metadata, "version", lambda name: "9.8.7" if name == "podgrove" else "wrong")
    with pytest.raises(SystemExit) as stopped:
        cli.parser().parse_args(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == "9.8.7"


def test_uninstalled_checkout_uses_release_source_version(monkeypatch):
    def missing(name):
        raise podgrove.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(podgrove.metadata, "version", missing)
    assert podgrove.package_version() == podgrove.__version__


def test_broken_distribution_metadata_is_not_hidden(monkeypatch):
    def broken(_):
        raise OSError("unreadable distribution metadata")
    monkeypatch.setattr(podgrove.metadata, "version", broken)
    with pytest.raises(OSError, match="unreadable"):
        podgrove.package_version()
