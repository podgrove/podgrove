"""The same reviewed boundary must govern Git export and source archives."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_sdist_selection_is_exactly_the_reviewed_public_files():
    files = json.loads((ROOT / "publication/public-files.json").read_text())["files"]
    rules = [line.strip() for line in (ROOT / "MANIFEST.in").read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert rules == ["global-exclude *", *[f"include {name}" for name in files]]


def test_every_runtime_module_and_browser_asset_is_reviewed_for_publication():
    files = set(json.loads((ROOT / "publication/public-files.json").read_text())["files"])
    runtime = {path.relative_to(ROOT).as_posix() for path in (ROOT / "podgrove").rglob("*")
               if path.is_file() and "__pycache__" not in path.parts
               and path.suffix in {".py", ".html", ".css", ".js", ".svg"}}
    assert runtime <= files, f"Runtime files missing from the public source boundary: {sorted(runtime - files)}"
