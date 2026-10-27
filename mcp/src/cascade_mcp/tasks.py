"""Bounded Redis dispatch and a single-slot Celery execution task."""
import os
import time
from celery import Celery
from cascade_mcp.jobs import JobError

app = Celery("cascade", broker=os.getenv("CELERY_BROKER_URL", "redis://cascade-redis:6379/0"),
             backend=os.getenv("CELERY_RESULT_BACKEND", "redis://cascade-redis:6379/1"))
visibility = int(os.getenv("MAX_RUNTIME_SECONDS", "3600")) + 600
app.conf.update(task_acks_late=True, task_reject_on_worker_lost=True,
                worker_prefetch_multiplier=1, task_ignore_result=True,
                broker_connection_timeout=2, broker_connection_retry_on_startup=True,
                broker_transport_options={"visibility_timeout": visibility, "socket_timeout": 2,
                                          "socket_connect_timeout": 2},
                result_backend_transport_options={"visibility_timeout": visibility},
                visibility_timeout=visibility, task_serializer="json", accept_content=["json"])


def dispatch(store, record):
    job_id = record["job_id"]
    with store.lock("dispatch-" + job_id):
        with store.lock("state-" + job_id):
            current = store.load(job_id)
            if current["status"] != "queued":
                return current
        uncertain = False
        try:
            app.send_task("cascade.execute", args=[job_id], task_id=current["celery_task_id"], retry=False)
        except Exception:
            uncertain = True
        with store.lock("state-" + job_id):
            current = store.load(job_id)
            current["last_dispatch"] = time.time()
            if current["status"] == "queued":
                current["dispatch_state"] = "uncertain" if uncertain else "confirmed"
            store.save(current)
            if uncertain and current["status"] == "queued":
                raise JobError("DISPATCH_UNCERTAIN", job_id, True)
            return current


@app.task(name="cascade.execute")
def execute(job_id):
    from cascade_mcp.worker import execute_job
    from cascade_mcp.settings import Settings
    execute_job(Settings(), job_id, os.environ["CASCADE_WORKER_ID"])
