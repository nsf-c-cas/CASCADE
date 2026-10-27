"""Generate provenance automatically inside the Docker metadata build stage."""
import hashlib
import json
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[2]
MCP = ROOT / "mcp"


def manifest():
    models = ROOT / "cascade-Jupyternotebook-SMILES/models/cascade/trained_model"
    model_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(models.glob("*.hdf5"))}
    digest = hashlib.sha256()
    sources = list((ROOT / "cascade-Jupyternotebook-SMILES/models/cascade").rglob("*.py"))
    sources += list((ROOT / "cascade-Jupyternotebook-SMILES/models/nfp").rglob("*.py"))
    sources += list((MCP / "src").rglob("*.py")) + list((MCP / "scripts").glob("*.py")) + [MCP / "pyproject.toml", MCP / "uv.lock"]
    sources += [ROOT / "cascade-Jupyternotebook-SMILES/models/cascade/preprocessor.p",
                MCP / "requirements-legacy.txt", MCP / "Dockerfile"]
    for path in sorted(sources):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    revision = os.getenv("SOURCE_REVISION", "unknown")
    preprocessor = ROOT / "cascade-Jupyternotebook-SMILES/models/cascade/preprocessor.p"
    content = {"source_revision": revision, "source_sha256": digest.hexdigest(),
               "preprocessor_sha256": hashlib.sha256(preprocessor.read_bytes()).hexdigest(),
               "model_sha256": model_hashes, "legacy_python": "3.5.6", "control_python": "3.12"}
    content["deployment_id"] = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    (MCP / "build").mkdir(exist_ok=True)
    (MCP / "build/deployment.json").write_text(json.dumps(content, indent=2) + "\n")
    print(content["deployment_id"])


if __name__ == "__main__":
    manifest()
