"""Regression coverage for source-layout configuration and build provenance."""
import importlib.util
import json
from pathlib import Path

import pytest

from cascade_mcp import settings


@pytest.mark.parametrize("manifest_path", ["build/deployment.json", "deployment.json"])
def test_manifest_lookup_from_src_package(tmp_path, monkeypatch, manifest_path):
    monkeypatch.setattr(settings, "__file__", str(tmp_path / "src/cascade_mcp/settings.py"))
    monkeypatch.delenv("DEPLOYMENT_MANIFEST", raising=False)
    path = tmp_path / manifest_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"deployment_id": "layout-test"}))
    assert settings.Settings().manifest == {"deployment_id": "layout-test"}


def test_manifest_hashes_moved_sources_and_scripts(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/deployment.py"
    spec = importlib.util.spec_from_file_location("deployment_layout_test", script)
    deployment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deployment)
    monkeypatch.setattr(deployment, "ROOT", tmp_path)
    monkeypatch.setattr(deployment, "MCP", tmp_path / "mcp")
    paths = ["cascade-Jupyternotebook-SMILES/models/cascade/preprocessor.p",
             "mcp/src/cascade_mcp/server.py", "mcp/scripts/predict_2d.py",
             "mcp/pyproject.toml", "mcp/uv.lock", "mcp/requirements-legacy.txt",
             "mcp/Dockerfile"]
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original")

    def digest():
        deployment.manifest()
        return json.loads((tmp_path / "mcp/build/deployment.json").read_text())["source_sha256"]

    previous = digest()
    for name in ("mcp/src/cascade_mcp/server.py", "mcp/scripts/predict_2d.py"):
        (tmp_path / name).write_text("changed")
        current = digest()
        assert current != previous
        previous = current
