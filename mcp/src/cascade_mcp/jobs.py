"""Authoritative filesystem records with stable locks; Redis is only a cache."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
import uuid

ID = re.compile(r"[A-Za-z0-9_-]{43}")
LOCK_STRIPES = 256
ACTIVE = {"queued", "running", "cancelling"}
TERMINAL = {"completed", "failed", "cancelled", "expired"}
MESSAGES = {
    "INVALID_INPUT": "Invalid input or identifier.", "JOB_NOT_FOUND": "Job record not found.",
    "REQUEST_ID_CONFLICT": "The retry key was already used with different input.",
    "DISPATCH_UNCERTAIN": "Dispatch is not confirmed. Retry with the same request_id or query this job.",
    "QUEUE_UNAVAILABLE": "Queue unavailable; no job was admitted.",
    "SERVICE_UNAVAILABLE": "Service temporarily unavailable; retry or contact the operator.",
    "ARTIFACT_UNAVAILABLE": "Required artifacts are missing or corrupted; the job will not be recomputed.",
    "INVALID_MOLECULE": "Molecule is outside the configured input scope or lacks atoms of the requested nucleus.",
    "PREDICTION_FAILED": "Prediction process failed. Diagnostics are retained privately.",
    "ARTIFACT_VALIDATION_FAILED": "Artifacts could not be validated or published.",
    "TIMEOUT": "The configured execution time limit was exceeded.",
    "WORKER_LOST": "The previous execution process ended unexpectedly; automatic recomputation is disabled.",
    "DISPATCH_FAILED": "Dispatch could not be recovered within the configured deadline.",
}


class JobError(Exception):
    def __init__(self, code, job_id=None, retryable=False):
        super().__init__(MESSAGES[code])
        self.code, self.job_id, self.retryable = code, job_id, retryable

    def response(self):
        result = {"ok": False, "error": {"code": self.code, "message": str(self), "retryable": self.retryable}}
        if self.job_id:
            result["job_id"] = self.job_id
        return result


def stamp(seconds=None):
    return datetime.fromtimestamp(time.time() if seconds is None else seconds, timezone.utc).isoformat()


def epoch(text):
    return datetime.fromisoformat(text).timestamp()


def atomic(path, value, mode=0o600):
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read(path):
    if path.is_symlink():
        raise JobError("SERVICE_UNAVAILABLE", retryable=True)
    return json.loads(path.read_text())


def normalize(smiles, nuclei, request_id, limit):
    if not isinstance(smiles, str) or not smiles.strip() or len(smiles) > limit or "\x00" in smiles:
        raise JobError("INVALID_INPUT")
    if not isinstance(nuclei, list) or not nuclei or any(not isinstance(n, str) for n in nuclei):
        raise JobError("INVALID_INPUT")
    aliases = {"1h": "1H", "hydrogen": "1H", "13c": "13C", "carbon": "13C"}
    try:
        values = sorted({aliases[n.lower().strip()] for n in nuclei})
    except KeyError:
        raise JobError("INVALID_INPUT")
    if len(values) != 1:
        raise JobError("INVALID_INPUT")
    if request_id is not None and (not isinstance(request_id, str) or
                                  not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id)):
        raise JobError("INVALID_INPUT")
    return {"smiles": smiles.strip(), "nuclei": values}


class Store:
    def __init__(self, cfg, cache=None):
        self.cfg, self.root, self.cache = cfg, cfg.jobs, cache
        self.root.mkdir(parents=True, exist_ok=True, mode=0o755)
        for name in (".locks", ".requests"):
            directory = self.root / name
            if directory.is_symlink():
                raise JobError("SERVICE_UNAVAILABLE", retryable=True)
            directory.mkdir(exist_ok=True, mode=0o700)

    @contextmanager
    def lock(self, key, blocking=True):
        # Keys are internal constants or hashes; never use a raw user-supplied retry key.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise JobError("INVALID_INPUT")
        if key not in {"admission", "worker"}:
            kind, _, value = key.partition("-")
            if kind not in {"retry", "execution", "state", "dispatch"} or not value:
                raise JobError("INVALID_INPUT")
            # Separate pools preserve lock ordering without unbounded per-request files.
            stripe = hashlib.sha256(key.encode()).digest()[0] % LOCK_STRIPES
            key = "%s-%d" % (kind, stripe)
        path = self.root / ".locks" / key
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                acquired = True
            except BlockingIOError:
                pass
            yield fd if acquired else None
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def directory(self, job_id):
        if not isinstance(job_id, str) or not ID.fullmatch(job_id):
            raise JobError("INVALID_INPUT")
        path = self.root / job_id
        if path.is_symlink():
            raise JobError("INVALID_INPUT")
        return path

    def load(self, job_id):
        path = self.directory(job_id) / "job.json"
        if not path.exists():
            raise JobError("JOB_NOT_FOUND")
        try:
            record = read(path)
            if record["job_id"] != job_id:
                raise ValueError("Invalid record")
            return record
        except (OSError, ValueError, KeyError):
            raise JobError("SERVICE_UNAVAILABLE", job_id, True) from None

    def save(self, record):
        record["revision"] += 1
        atomic(self.directory(record["job_id"]) / "job.json", record)
        if self.cache:
            try:
                self.cache.setex("cascade:job:" + record["job_id"], int(self.cfg.records), json.dumps(record))
            except Exception:
                pass  # Durable record already committed; Redis never overrides it.

    def ids(self):
        return sorted(p.name for p in self.root.iterdir() if ID.fullmatch(p.name) and (p / "job.json").exists())

    def ready(self):
        if (self.root / ".admission-closed").exists() or (self.root / ".blocked").exists():
            raise JobError("SERVICE_UNAVAILABLE", retryable=True)
        try:
            info = read(self.root / ".worker.json")
            if (not info["ready"] or time.time() - info["heartbeat"] > 10 or
                    info["deployment_id"] != self.cfg.manifest["deployment_id"]):
                raise ValueError("Worker not ready")
            if shutil.disk_usage(self.root).free < self.cfg.min_free:
                raise ValueError("Insufficient disk space")
        except (OSError, ValueError, KeyError):
            raise JobError("SERVICE_UNAVAILABLE", retryable=True) from None
        return info

    def create(self, request, request_id):
        digest = hashlib.sha256(request_id.encode()).hexdigest() if request_id else "unkeyed"
        with self.lock("retry-" + digest):
            index = self.root / ".requests" / digest if request_id else None
            reserved = read(index) if index and index.exists() else None
            reused = reserved is not None
            if reserved:
                if reserved["request"] != request:
                    raise JobError("REQUEST_ID_CONFLICT")
                if (self.directory(reserved["job_id"]) / "job.json").exists():
                    return self.load(reserved["job_id"]), True
            with self.lock("admission"):
                worker = self.ready()
                records = [self.load(i) for i in self.ids()]
                active = [r for r in records if r["status"] in ACTIVE]
                if len(active) >= self.cfg.max_queue:
                    raise JobError("SERVICE_UNAVAILABLE", retryable=True)
                if reserved is None:
                    reserved = {"job_id": secrets.token_urlsafe(32), "request": request, "request_id": request_id,
                                "created_at": stamp()}
                    if index:
                        atomic(index, reserved)
                job_id = reserved["job_id"]
                directory = self.directory(job_id)
                directory.mkdir(exist_ok=True, mode=0o755)
                for name in ("input", "logs", "work"):
                    sub = directory / name
                    if sub.is_symlink():
                        raise JobError("SERVICE_UNAVAILABLE", retryable=True)
                    sub.mkdir(exist_ok=True, mode=0o700)
                atomic(directory / "input/request.json", request)
                record = {"job_id": job_id, "revision": 0, "status": "queued", "stage": "queued",
                          "created_at": reserved["created_at"], "started_at": None, "finished_at": None,
                          "expires_at": None, "cancel_requested": False, "termination_reason": None,
                          "celery_task_id": str(uuid.uuid4()), "dispatch_state": "pending", "last_dispatch": 0,
                          "publication_state": None, "error": None, "request_hash": digest if index else None,
                          "request": request, "deployment_id": self.cfg.manifest["deployment_id"],
                          "provenance": dict(self.cfg.manifest, worker_image=self.cfg.worker_image,
                                             control_image=self.cfg.control_image, runtime=worker["runtime"])}
                self.save(record)
                return record, reused

    def finish(self, record, status, code=None):
        record["status"], record["stage"] = status, status
        if not record["finished_at"]:
            record["finished_at"] = stamp()
            record["expires_at"] = stamp(epoch(record["finished_at"]) + self.cfg.retention)
        if code:
            record["error"] = {"code": code, "message": MESSAGES[code]}
        self.save(record)

    def query(self, job_id):
        with self.lock("state-" + self.directory(job_id).name):
            record = self.load(job_id)
            if record["publication_state"] == "committing":
                raise JobError("SERVICE_UNAVAILABLE", job_id, True)
            if record["expires_at"] and time.time() >= epoch(record["expires_at"]):
                record["status"] = "expired"
            return record

    def cancel(self, job_id):
        with self.lock("state-" + self.directory(job_id).name):
            record = self.load(job_id)
            if record["publication_state"] == "committing":
                raise JobError("SERVICE_UNAVAILABLE", job_id, True)
            if record["expires_at"] and time.time() >= epoch(record["expires_at"]):
                record["status"] = "expired"
            accepted = record["status"] in ACTIVE and not record["cancel_requested"]
            if accepted:
                record["cancel_requested"] = True
                record["termination_reason"] = "cancel_requested"
                if record["status"] == "queued":
                    self.finish(record, "cancelled")
                else:
                    record["status"] = "cancelling"
                    self.save(record)
            return {"ok": True, "job_id": job_id, "status": record["status"],
                    "cancel_accepted": accepted, "cancel_requested": record["cancel_requested"]}
