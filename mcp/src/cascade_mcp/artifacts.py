"""Validate immutable artifact manifests, package staging, and publish atomically."""
import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
import zipfile
from PIL import Image
from cascade_mcp.jobs import JobError, atomic, epoch

NAMES = ("weighted_shift.csv", "conformers_shift.csv", "conformers.sdf", "prediction_2d.png", "prediction_2d.svg")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(directory, nucleus):
    work = directory / "work"
    summary = json.loads((work / "validation.json").read_text())
    staging = directory / "staging"
    staging.mkdir(mode=0o700)
    for name in NAMES:
        source = work / nucleus / name
        if source.is_symlink() or not source.is_file() or source.stat().st_size == 0:
            raise ValueError("Missing or unsafe artifact")
        if name.endswith("png"):
            with Image.open(source) as image:
                image.verify()
        shutil.copyfile(source, staging / (nucleus + "_" + name))
    with zipfile.ZipFile(staging / "results.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in NAMES:
            archive.write(staging / (nucleus + "_" + name), nucleus + "/" + name)
    manifest = [{"name": p.name, "mime_type": "chemical/x-mdl-sdfile" if p.suffix == ".sdf" else
                 (mimetypes.guess_type(p.name)[0] or "application/octet-stream"),
                 "size": p.stat().st_size, "sha256": digest(p)} for p in sorted(staging.iterdir())]
    atomic(directory / "prepared.json", {"results": summary, "artifacts": manifest})


def valid(directory, manifest):
    try:
        if directory.is_symlink() or not directory.is_dir() or not manifest:
            return False
        allowed = re.compile(r"(?:(?:1H|13C)_(?:weighted_shift\.csv|conformers_shift\.csv|conformers\.sdf|prediction_2d\.(?:png|svg))|results\.zip)")
        if any(not allowed.fullmatch(item["name"]) for item in manifest):
            return False
        expected = {item["name"] for item in manifest}
        if {p.name for p in directory.iterdir()} != expected:
            return False
        for item in manifest:
            path = directory / item["name"]
            if path.is_symlink() or not path.is_file() or path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
                return False
        return True
    except OSError:
        return False


def publish(store, record):
    """Caller holds execution and state locks. 'committing' is durable before rename."""
    directory = store.directory(record["job_id"])
    public, staging = directory / "public", directory / "staging"
    if not public.exists():
        if not valid(staging, record["artifacts"]):
            raise ValueError("Invalid staging manifest")
        for file in staging.iterdir():
            file.chmod(0o644)
        staging.chmod(0o755)
        os.replace(staging, public)
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if not valid(public, record["artifacts"]):
        raise ValueError("Invalid public manifest")
    record["publication_state"] = "complete"
    store.finish(record, "completed")
    shutil.rmtree(directory / "work", ignore_errors=True)


def result(store, record):
    data = {"ok": True, "job_id": record["job_id"], "status": record["status"], "files": [],
            "finished_at": record["finished_at"], "expires_at": record["expires_at"]}
    if record.get("error"):
        data["error"] = record["error"]
    if record["status"] != "completed":
        return data
    if not valid(store.directory(record["job_id"]) / "public", record["artifacts"]):
        if time.time() >= epoch(record["expires_at"]):
            data["status"] = "expired"
            return data
        raise JobError("ARTIFACT_UNAVAILABLE", record["job_id"])
    data.update(summary="CASCADE chemical-shift predictions for a candidate structure; not structure identification or an NMR spectrum.",
                results=record["results"], provenance=record["provenance"])
    data["files"] = [{"name": item["name"], "mime_type": item["mime_type"],
                      "url": store.cfg.base_url + "/results/" + record["job_id"] + "/" + item["name"]}
                     for item in record["artifacts"]]
    return data
