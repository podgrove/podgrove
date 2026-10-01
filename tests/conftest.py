"""Shared fixtures for the offline test suite."""
import pytest


@pytest.fixture(autouse=True)
def isolated_state_home(tmp_path_factory, monkeypatch):
    """Every test reads and writes Podgrove state under its own temporary directory, never the developer's."""
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path_factory.mktemp("podgrove-state")))
