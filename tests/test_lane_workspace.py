"""Two unchanged repository Compose overlays within one explicit lane boundary."""
import json

from podgrove.compose import Compose
from podgrove.config import load_config


def test_real_compose_normalizes_two_repository_lane_without_external_paths(tmp_path):
    backend, web = tmp_path / "backend", tmp_path / "web"
    backend.mkdir()
    web.mkdir()
    (backend / "src").mkdir()
    (web / "config").mkdir()
    (web / "config" / "local.conf").write_text("test")
    (backend / "compose.yml").write_text("name: lane-test\nservices:\n  api:\n    image: busybox:1.37\n    volumes: ['./src:/app']\n")
    (web / "compose.overlay.yml").write_text('services:\n  web:\n    image: nginx:alpine\n    volumes: ["${WEB_CONFIG_DIR:?set WEB_CONFIG_DIR}/local.conf:/etc/nginx/conf.d/default.conf:ro"]\n')
    (tmp_path / "lane.env").write_text(f"WEB_CONFIG_DIR={web / 'config'}\n")
    (tmp_path / "podgrove.yml").write_text("compose:\n  project_directory: backend\n  files: [backend/compose.yml, web/compose.overlay.yml]\n  env_file: lane.env\n")
    compose = Compose(load_config(tmp_path))
    first = compose.model()
    second = compose.model()
    compose.validate(first)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["services"]["api"]["volumes"][0]["source"] == str(backend / "src")
    assert first["services"]["web"]["volumes"][0]["source"] == str(web / "config" / "local.conf")
    assert set(compose.sync_paths(first)) == {backend / "src", web / "config" / "local.conf"}
    assert compose.config.root == tmp_path and compose.config.project_directory == backend
