"""Operational limits, not validated scientific applicability limits."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path


@dataclass
class Settings:
    jobs: Path = field(default_factory=lambda: Path(os.getenv("JOBS_DIR", "/data/jobs")))
    broker: str = field(default_factory=lambda: os.getenv("CELERY_BROKER_URL", "redis://cascade-redis:6379/0"))
    backend: str = field(default_factory=lambda: os.getenv("CELERY_RESULT_BACKEND", "redis://cascade-redis:6379/1"))
    base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://localhost/cascade").rstrip("/"))
    legacy: str = field(default_factory=lambda: os.getenv("LEGACY_PYTHON", "/opt/conda/bin/python"))
    models: Path = field(default_factory=lambda: Path(os.getenv("MODELS_DIR", "/src/cascade-Jupyternotebook-SMILES/models")))
    runtime: float = field(default_factory=lambda: float(os.getenv("MAX_RUNTIME_SECONDS", "3600")))
    grace: float = field(default_factory=lambda: float(os.getenv("CANCEL_GRACE_SECONDS", "10")))
    retention: float = field(default_factory=lambda: float(os.getenv("RESULT_RETENTION_SECONDS", "86400")))
    records: float = field(default_factory=lambda: float(os.getenv("RECORD_RETENTION_SECONDS", "604800")))
    interval: float = field(default_factory=lambda: float(os.getenv("CLEANUP_INTERVAL_SECONDS", "300")))
    dispatch_deadline: float = field(default_factory=lambda: float(os.getenv("DISPATCH_DEADLINE_SECONDS", "60")))
    max_queue: int = field(default_factory=lambda: int(os.getenv("MAX_QUEUED_JOBS", "20")))
    max_atoms: int = field(default_factory=lambda: int(os.getenv("MAX_ATOMS", "100")))
    max_smiles: int = 4096
    min_free: int = field(default_factory=lambda: int(os.getenv("MIN_FREE_BYTES", "67108864")))
    worker_image: str = field(default_factory=lambda: "cascade-worker:local")
    control_image: str = field(default_factory=lambda: "cascade-control:local")
    manifest: dict = field(default_factory=lambda: json.loads(
        Path(os.getenv("DEPLOYMENT_MANIFEST", str(Path(__file__).parents[2] / "deployment.json"
             if (Path(__file__).parents[2] / "deployment.json").exists()
             else Path(__file__).parents[2] / "build/deployment.json"))).read_text()))

    def __post_init__(self):
        if self.records <= self.retention or min(self.runtime, self.grace, self.retention,
                                               self.interval, self.dispatch_deadline) <= 0:
            raise ValueError("Invalid timing settings; record retention must exceed artifact retention")
        if min(self.max_atoms, self.max_queue, self.min_free) < 1:
            raise ValueError("Limits must be positive")
