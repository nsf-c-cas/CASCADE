import json
import time
import pytest

from cascade_mcp.jobs import Store, atomic
from cascade_mcp.settings import Settings


@pytest.fixture
def store(tmp_path):
    cfg = Settings(jobs=tmp_path, manifest={"deployment_id": "test-deployment", "source_revision": "test", "model_sha256": {}},
                   worker_image="sha256:" + "a" * 64, control_image="sha256:" + "b" * 64,
                   base_url="https://test.invalid/cascade", min_free=1, retention=10, records=20,
                   grace=0.1, runtime=1)
    store = Store(cfg)
    atomic(tmp_path / ".worker.json", {"ready": True, "heartbeat": time.time(), "deployment_id": "test-deployment",
                                      "worker_image": cfg.worker_image, "runtime": {"python": "3.5.6"}})
    return store


@pytest.fixture
def job(store):
    return store.create({"smiles": "CCO", "nuclei": ["13C"]}, "retry-key-123456789")[0]
