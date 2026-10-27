"""Worker-local process ownership; never interpret PIDs in maintenance containers."""
import os
from pathlib import Path
import signal
import time


def identity(pid):
    fields = Path("/proc/%s/stat" % pid).read_text().rsplit(") ", 1)[1].split()
    return {"pid": pid, "start": fields[19], "group": int(fields[2])}


def marked(worker_id, job_id=None):
    needles = [b"CASCADE_WORKER_ID=" + worker_id.encode()]
    if job_id:
        needles.append(b"CASCADE_JOB_ID=" + job_id.encode())
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            before = identity(int(entry.name))
            values = (entry / "environ").read_bytes().split(b"\x00")
            if all(n in values for n in needles) and any(v.startswith(b"CASCADE_JOB_ID=") for v in values):
                after = identity(int(entry.name))
                if before["start"] == after["start"]:
                    result.append(after)
        except (OSError, ValueError, IndexError):
            continue
    return result


def stop_owned(worker_id, job_id, grace):
    def send(sig):
        groups = set()
        for process in marked(worker_id, job_id):
            try:
                current = identity(process["pid"])
                if current["start"] == process["start"] and current["group"] == process["group"]:
                    groups.add(process["group"])
            except (OSError, ValueError):
                pass
        for group in groups:
            if group <= 1 or group == os.getpgrp():
                raise RuntimeError("Unsafe process group")
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass
    send(signal.SIGTERM)
    end = time.monotonic() + grace
    while marked(worker_id, job_id) and time.monotonic() < end:
        time.sleep(0.05)
    send(signal.SIGKILL)
    end = time.monotonic() + 5
    while marked(worker_id, job_id) and time.monotonic() < end:
        time.sleep(0.05)
    if marked(worker_id, job_id):
        raise RuntimeError("Owned processes remain; refusing to confirm termination")
