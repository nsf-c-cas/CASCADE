"""Synchronous application boundary shared by MCP and lifecycle tests."""
import time
from cascade_mcp.artifacts import result
from cascade_mcp.jobs import epoch, normalize
from cascade_mcp.tasks import dispatch


class Service:
    def __init__(self, store, send=dispatch):
        self.store, self.send = store, send

    def submit(self, smiles, nuclei, request_id=None):
        request = normalize(smiles, nuclei, request_id, self.store.cfg.max_smiles)
        record, reused = self.store.create(request, request_id)
        if record["status"] == "queued" and record["dispatch_state"] != "confirmed":
            record = self.send(self.store, record)
        status = "expired" if record["expires_at"] and time.time() >= epoch(record["expires_at"]) else record["status"]
        return {"ok": True, "job_id": record["job_id"], "status": status,
                "reused": reused}

    def status(self, job_id):
        record = self.store.query(job_id)
        keys = ("job_id", "status", "stage", "created_at", "started_at", "finished_at", "expires_at", "error")
        return dict({"ok": True}, **{key: record[key] for key in keys})

    def result(self, job_id):
        return result(self.store, self.store.query(job_id))

    def cancel(self, job_id):
        return self.store.cancel(job_id)
