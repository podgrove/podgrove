"""Opt-in missing-file recovery on one disposable, owned Docker-in-Docker engine."""
import os
from pathlib import Path
import shutil
import time

import pytest

from podgrove.compose import Compose
from podgrove.config import load_config
from podgrove.runtime import StartupIncomplete, launch_stack, readiness, service_status
from scripts.e2e import Harness


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("PODGROVE_RUN_DOCKER_E2E") != "1", reason="set PODGROVE_RUN_DOCKER_E2E=1")
def test_missing_file_reup_remirrors_and_recovers_exited_and_unhealthy_services(tmp_path):
    fixture = Path(__file__).resolve().parents[1] / "fixtures/recovery"
    worktree = tmp_path / "worktree"
    shutil.copytree(fixture, worktree)
    harness = Harness(tmp_path / "evidence", mongo=False)
    harness.require_local_engine()
    try:
        env, _, _ = harness.engine("recovery")
        compose = Compose(load_config(worktree, files=["compose.yml"]))
        compose.recover_existing = True
        model = compose.model()
        with pytest.raises(StartupIncomplete) as failed:
            launch_stack(compose, model, env, "012345abcdef", timeout=30)
        harness.syncers.append(failed.value.sync)
        deadline = time.monotonic() + 15
        while True:
            rows = service_status(compose, env)
            states = {row["Service"]: row for row in rows}
            if states["exited"]["State"] == "exited" and states["unhealthy"]["Health"] == "unhealthy":
                break
            assert time.monotonic() < deadline
            time.sleep(0.2)
        healthy_id = states["healthy"]["ID"]
        assert states["healthy"]["State"] == "running"
        harness.record("partial_startup_retains_healthy_service", healthy_id=healthy_id)
        failed.value.sync.close()
        harness.syncers.remove(failed.value.sync)
        (worktree / "content/required.txt").write_text("arrived after failed startup\n")
        sync, _, rows = launch_stack(compose, model, env, "012345abcdef", timeout=60)
        harness.syncers.append(sync)
        assert readiness(model, rows) == (True, [])
        assert next(row["ID"] for row in rows if row["Service"] == "healthy") == healthy_id
        copied = harness.run(compose.command("exec", "-T", "exited", "cat", "/fixture/required.txt"), env)
        assert copied.stdout == "arrived after failed startup\n"
        harness.record("reup_remirrors_then_recovers_failed_services", services=len(rows), healthy_id_unchanged=True)
    finally:
        harness.cleanup()
    harness.save("passed")
