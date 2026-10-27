"""Dispatch/publication recovery and repeatable cleanup, independent of computation."""
from contextlib import nullcontext
import re
import shutil
import time
import traceback
from cascade_mcp.artifacts import publish
from cascade_mcp.jobs import ACTIVE, JobError, Store, atomic, epoch, read
from cascade_mcp.settings import Settings
from cascade_mcp.tasks import dispatch


def tick(store, cleanup=True):
    for index in (store.root / ".requests").iterdir():
        if not re.fullmatch(r"[a-f0-9]{64}", index.name):
            continue
        reserved = read(index)
        if not (store.directory(reserved["job_id"]) / "job.json").exists():
            # A reserved retry key precedes job creation; resume that exact reservation.
            if reserved.get("request_id"):
                try:
                    store.create(reserved["request"], reserved["request_id"])
                except JobError as error:
                    if error.code != "SERVICE_UNAVAILABLE":
                        raise
    records = [store.load(i) for i in store.ids()]
    executing = any(r["status"] in {"running", "cancelling"} for r in records)
    for record in records:
        job_id = record["job_id"]
        if record["status"] == "queued":
            if record["dispatch_state"] != "confirmed" and time.time() - epoch(record["created_at"]) > store.cfg.dispatch_deadline:
                with store.lock("state-" + job_id):
                    latest = store.load(job_id)
                    if latest["status"] == "queued" and latest["dispatch_state"] != "confirmed":
                        store.finish(latest, "failed", "DISPATCH_FAILED")
            elif time.time() - record["last_dispatch"] > 10 and (not executing or record["dispatch_state"] != "confirmed"):
                try:
                    dispatch(store, record)
                except JobError:
                    pass
        digest = record["request_hash"]
        with store.lock("retry-" + digest) if digest else nullcontext():
            with store.lock("execution-" + job_id, blocking=False) as execution:
                if execution is None:
                    continue
                with store.lock("state-" + job_id):
                    latest = store.load(job_id)
                    directory = store.directory(job_id)
                    if latest["publication_state"] == "committing" and not latest.get("cleanup_intent"):
                        try:
                            if time.time() - epoch(latest["commit_at"]) > 60:
                                raise ValueError("Publication recovery deadline exceeded")
                            publish(store, latest)
                        except (ValueError, OSError):
                            shutil.rmtree(directory / "public", ignore_errors=True)
                            latest["publication_state"] = "failed"
                            store.finish(latest, "failed", "ARTIFACT_VALIDATION_FAILED")
                    if latest["status"] in ACTIVE:
                        continue  # Maintenance never infers process loss from its own PID namespace.
                    if not cleanup:
                        continue
                    if latest["expires_at"] and time.time() >= epoch(latest["expires_at"]):
                        latest.update(cleanup_intent=True, status="expired")
                        store.save(latest)
                        for name in ("public", "staging", "work", "logs", "input"):
                            shutil.rmtree(directory / name, ignore_errors=False) if (directory / name).exists() else None
                    if latest["finished_at"] and time.time() >= epoch(latest["finished_at"]) + store.cfg.records:
                        if digest:
                            (store.root / ".requests" / digest).unlink(missing_ok=True)
                        if store.cache:
                            try:
                                store.cache.delete("cascade:job:" + job_id)
                            except Exception:
                                pass
                        shutil.rmtree(directory)
                        # Stable lock files are intentionally retained online.


def main():
    store = Store(Settings())
    next_cleanup = 0
    while True:
        try:
            now = time.monotonic()
            cleanup = now >= next_cleanup
            tick(store, cleanup=cleanup)
            if cleanup:
                next_cleanup = now + store.cfg.interval
        except Exception:
            traceback.print_exc()
            atomic(store.root / ".blocked", {"reason": "Maintenance recovery requires investigation"})
        time.sleep(min(store.cfg.interval, 10))  # Dispatch recovery is bounded independently of retention.


if __name__ == "__main__":
    main()
