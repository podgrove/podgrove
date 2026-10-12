import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location("diagnostics_acceptance", Path(__file__).resolve().parents[1] / "scripts/check_startup_diagnostics.py")
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


def test_plan_never_creates_resources(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(acceptance.Runner, "run", Mock(side_effect=AssertionError("no mutation")))
    output = tmp_path / "evidence"
    assert acceptance.main(["--podgrove-bin", "/missing", "--context", "explicit", "--namespace", "tests",
                            "--storage-class", "approved", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["execute"] is False
    assert not output.exists()


@pytest.mark.parametrize("option,value", [("--podgrove-bin", "relative"), ("--context", ""),
                                          ("--namespace", "kube-system"), ("--storage-class", ""),
                                          ("--output", "relative")])
def test_execution_requires_explicit_valid_target(tmp_path, monkeypatch, option, value):
    monkeypatch.setattr(acceptance.Runner, "run", Mock(side_effect=AssertionError("no mutation")))
    args = {"--podgrove-bin": sys.executable, "--context": "explicit", "--namespace": "tests",
            "--storage-class": "approved", "--output": str(tmp_path / "evidence")}
    args[option] = value
    with pytest.raises(SystemExit) as error:
        acceptance.main([part for pair in args.items() for part in pair] + ["--execute"])
    assert error.value.code == 2


@pytest.mark.parametrize("output,streamed", [
    ("#8 [readable 2/2] RUN print('build probe started'); print('build probe finished')", False),
    ("#8 0.123 build probe started\n", True),
    ("#8 0.123 build probe started\n#8 30.123 build probe finished\n", False),
    ("build probe started\n", True),
    ("build probe started\nbuild probe finished\n", False),
])
def test_stream_proof_requires_actual_output_before_build_finishes(output, streamed):
    assert acceptance.build_is_streaming(output) is streamed
