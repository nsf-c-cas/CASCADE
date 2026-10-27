"""Single-slot Celery supervisor and monitored per-job subprocess groups."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import traceback
import uuid
from cascade_mcp.artifacts import digest, publish
from cascade_mcp.jobs import ACTIVE, JobError, Store, atomic, read, stamp
from cascade_mcp.processes import identity, marked, stop_owned
from cascade_mcp.settings import Settings


def execute_job(cfg, job_id, worker_id):
    store = Store(cfg)
    directory = store.directory(job_id)
    process = None
    with store.lock("execution-" + job_id, blocking=False) as execution:
        if execution is None:
            return
        try:
            with store.lock("state-" + job_id):
                record = store.load(job_id)
                if record["status"] != "queued":
                    return
                record.update(status="running", stage="starting", started_at=stamp(), owner=worker_id,
                              dispatch_state="confirmed")
                store.save(record)
            with (directory / "logs/stdout.log").open("ab") as stdout, (directory / "logs/stderr.log").open("ab") as stderr:
                started = time.monotonic()
                with store.lock("state-" + job_id):
                    record = store.load(job_id)
                    if record["cancel_requested"]:
                        store.finish(record, "cancelled")
                        return
                    env = dict(os.environ, CASCADE_WORKER_ID=worker_id, CASCADE_JOB_ID=job_id,
                               CASCADE_LOCK_FD=str(execution), PYTHONPATH=str(Path(__file__).parents[1]))
                    env.pop("PYTHONHOME", None)
                    process = subprocess.Popen([sys.executable, "-m", "cascade_mcp.pipeline",
                                                str(directory), cfg.legacy, str(cfg.models), str(cfg.max_atoms)],
                                               env=env, start_new_session=True, pass_fds=(execution,),
                                               stdout=stdout, stderr=stderr)
                    record.update(process=identity(process.pid), stage="prediction")
                    store.save(record)
                reason = None
                while process.poll() is None:
                    with store.lock("state-" + job_id):
                        record = store.load(job_id)
                        if record["cancel_requested"]:
                            reason = "cancelled"
                        elif time.monotonic() - started >= cfg.runtime:
                            reason = "TIMEOUT"
                        elif record["termination_reason"] == "worker_lost":
                            reason = "WORKER_LOST"
                    if reason:
                        stop_owned(worker_id, job_id, cfg.grace)
                        break
                    time.sleep(0.2)
                process.wait(timeout=5)
                stop_owned(worker_id, job_id, cfg.grace)  # Reap escaped/orphaned descendants before committing.
                stderr.write(("\nPipeline exit code: %s\n" % process.returncode).encode())
                stderr.flush()
                with store.lock("state-" + job_id):
                    record = store.load(job_id)
                    record["exit_code"] = process.returncode
                    if record["cancel_requested"] or reason == "cancelled":
                        store.finish(record, "cancelled")
                    elif reason or process.returncode:
                        code = reason or ({20: "INVALID_MOLECULE", 21: "ARTIFACT_VALIDATION_FAILED"}.get(process.returncode, "PREDICTION_FAILED"))
                        record["termination_reason"] = "timeout" if code == "TIMEOUT" else record["termination_reason"]
                        store.finish(record, "failed", code)
                    else:
                        prepared = read(directory / "prepared.json")
                        record.update(prepared, publication_state="committing", commit_at=stamp())
                        store.save(record)  # Durable cancellation cutoff before publication.
                        publish(store, record)
        except JobError as error:
            if error.code != "JOB_NOT_FOUND":
                atomic(store.root / ".blocked", {"reason": "Job execution requires operator investigation"})
                raise
        except Exception:
            traceback.print_exc()
            if process:
                stop_owned(worker_id, job_id, cfg.grace)
                process.wait(timeout=5)
            atomic(store.root / ".blocked", {"reason": "Execution/persistence failure"})
            with store.lock("state-" + job_id):
                record = store.load(job_id)
                if record["publication_state"] != "committing" and record["status"] in ACTIVE:
                    store.finish(record, "failed", "PREDICTION_FAILED")
        finally:
            if process:
                stop_owned(worker_id, job_id, cfg.grace)
                process.wait(timeout=5)


def preflight(cfg):
    for path in (Path(__file__).parent / "pipeline.py", Path(__file__).parent / "legacy_adapter.py",
                 Path("/opt/cascade-test/predict_2d.py")):
        if not path.is_file():
            raise ValueError("Required execution adapter/CLI is missing")
    for name, expected in cfg.manifest["model_sha256"].items():
        if digest(cfg.models / "cascade/trained_model" / name) != expected:
            raise ValueError("Model hash differs from the build manifest")
    if digest(cfg.models / "cascade/preprocessor.p") != cfg.manifest["preprocessor_sha256"]:
        raise ValueError("Preprocessor hash differs from the build manifest")
    env = dict(os.environ, PYTHONPATH=str(cfg.models))
    env.pop("PYTHONHOME", None)
    output = subprocess.check_output([cfg.legacy, "-c", "import sys,json; from rdkit import rdBase; import keras,tensorflow; print(json.dumps({'python':sys.version.split()[0],'rdkit':rdBase.rdkitVersion,'keras':keras.__version__,'tensorflow':tensorflow.__version__}))"],
                                     cwd=cfg.models, env=env, text=True, timeout=60)
    versions = json.loads(output.splitlines()[-1])
    if not versions["python"].startswith("3.5."):
        raise ValueError("Wrong legacy interpreter")
    if any(versions[key] != value for key, value in {"keras": "2.2.4", "tensorflow": "1.12.0", "rdkit": "2018.03.3"}.items()):
        raise ValueError("Legacy scientific dependencies differ from the validated baseline")
    versions["control_python"] = sys.version.split()[0]
    return versions


def reconcile_local(store, worker_id=None):
    """Only the owning worker supervisor may inspect/terminate local PIDs."""
    for job_id in store.ids():
        record = store.load(job_id)
        if record["status"] not in {"running", "cancelling"} or record["publication_state"] == "committing":
            continue
        owner = record["owner"]
        if worker_id and owner != worker_id:
            continue
        stop_owned(owner, job_id, store.cfg.grace)
        with store.lock("execution-" + job_id, blocking=False) as fd:
            if fd is None:
                raise RuntimeError("Execution ownership remains uncertain")
            with store.lock("state-" + job_id):
                record = store.load(job_id)
                if record["status"] in ACTIVE:
                    store.finish(record, "failed", "WORKER_LOST")


def restart_jobs(store):
    """Fail unfinished work from the previous worker; preserve completed results."""
    from cascade_mcp.maintenance import tick
    with store.lock("admission"):
        reconcile_local(store)
        for job_id in store.ids():
            with store.lock("execution-" + job_id, blocking=False) as execution:
                if execution is None:
                    raise RuntimeError("Previous computation still owns its execution lock")
                with store.lock("state-" + job_id):
                    record = store.load(job_id)
                    if record["status"] in ACTIVE and record["publication_state"] != "committing":
                        store.finish(record, "failed", "WORKER_LOST")
    tick(store)  # Finish validated publications and surface corrupt state before admission.
    with store.lock("admission"):
        (store.root / ".admission-closed").unlink(missing_ok=True)
        (store.root / ".blocked").unlink(missing_ok=True)


def main():
    cfg = Settings()
    store = Store(cfg)
    worker_id = uuid.uuid4().hex
    stopped = False
    child = None

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with store.lock("worker", blocking=False) as singleton:
        if singleton is None:
            raise RuntimeError("Only one CASCADE worker supervisor is supported")
        info = {"worker_id": worker_id, "deployment_id": cfg.manifest["deployment_id"],
                "worker_image": cfg.worker_image, "ready": False, "runtime": {}, "heartbeat": time.time()}
        try:
            atomic(store.root / ".worker.json", info)
            info["runtime"] = preflight(cfg)
            restart_jobs(store)
            env = dict(os.environ, CASCADE_WORKER_ID=worker_id)
            child = subprocess.Popen([sys.executable, "-m", "celery", "-A", "cascade_mcp.tasks:app", "worker",
                                      "--pool=solo", "--concurrency=1", "--loglevel=info", "--without-gossip",
                                      "--without-mingle"], env=env)
            info["celery_pid"] = child.pid
            while not stopped and child.poll() is None:
                info.update(ready=shutil.disk_usage(store.root).free >= cfg.min_free, heartbeat=time.time())
                atomic(store.root / ".worker.json", info)
                time.sleep(1)
        finally:
            info.update(ready=False, heartbeat=time.time())
            atomic(store.root / ".worker.json", info)
            if child:
                for job_id in store.ids():
                    with store.lock("state-" + job_id):
                        record = store.load(job_id)
                        if record.get("owner") == worker_id and record["status"] in ACTIVE:
                            record["termination_reason"] = "worker_lost"
                            store.save(record)
                    stop_owned(worker_id, job_id, cfg.grace)
                child.terminate()
                try:
                    child.wait(timeout=cfg.grace + 5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                reconcile_local(store, worker_id)
    if not stopped:
        raise RuntimeError("Celery execution process ended unexpectedly")


if __name__ == "__main__":
    main()
