"""Opt-in real Docker suite; invoke scripts/e2e.py directly for retained evidence."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("PODGROVE_RUN_DOCKER_E2E") != "1", reason="set PODGROVE_RUN_DOCKER_E2E=1")
def test_remote_engine_acceptance(tmp_path):
    root = Path(__file__).resolve().parents[2]
    destination = Path(os.environ["PODGROVE_DOCKER_E2E_OUTPUT"]) if os.environ.get("PODGROVE_DOCKER_E2E_OUTPUT") else tmp_path
    assert not destination.exists() or not any(destination.iterdir()), "Preserve existing integration evidence"
    subprocess.run([sys.executable, str(root / "scripts/e2e.py"), "--mongo", "--output", str(destination)], check=True, timeout=1200)
