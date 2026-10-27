import shutil
import time
from unittest.mock import patch
import pytest
from cascade_mcp.artifacts import build, publish, result, valid
from cascade_mcp.jobs import JobError, atomic, read, stamp
from cascade_mcp.maintenance import tick


def stage(store, job):
    directory = store.directory(job["job_id"])
    # Standalone outputs are not required on another checkout; create a valid tiny fixture.
    from PIL import Image
    folder = directory / "work/13C"
    folder.mkdir(parents=True)
    for name in ("weighted_shift.csv", "conformers_shift.csv", "conformers.sdf", "prediction_2d.svg"):
        (folder / name).write_text("validated fixture")
    Image.new("RGB", (2, 2)).save(folder / "prediction_2d.png")
    atomic(directory / "work/validation.json", {"13C": {"atom_count": 1, "conformer_count": 2, "unit": "ppm"}})
    build(directory, "13C")
    return read(directory / "prepared.json")


def test_execution_lock_prevents_recovery_from_publishing_an_active_job(store, job):
    job.update(stage(store, job), status="running", publication_state="committing", commit_at=stamp())
    store.save(job)
    with store.lock("execution-" + job["job_id"]):
        tick(store)
    assert not (store.directory(job["job_id"]) / "public").exists()
    tick(store)
    assert store.query(job["job_id"])["status"] == "completed"


@pytest.mark.parametrize("rename_first", [False, True])
def test_interrupted_publication_recovers_same_manifest_without_recompute(store, job, rename_first):
    directory = store.directory(job["job_id"])
    job.update(stage(store, job), status="running", owner="worker", publication_state="committing", commit_at=stamp())
    store.save(job)
    if rename_first:
        (directory / "staging").rename(directory / "public")
    tick(store)
    completed = store.query(job["job_id"])
    assert completed["status"] == "completed"
    assert result(store, completed)["files"]
    assert not store.cancel(job["job_id"])["cancel_accepted"]


def test_unrecoverable_publication_withdraws_public_and_confirms_failure(store, job):
    directory = store.directory(job["job_id"])
    job.update(stage(store, job), status="running", owner="worker", publication_state="committing", commit_at=stamp())
    store.save(job)
    (directory / "staging").rename(directory / "public")
    (directory / "public/results.zip").write_bytes(b"corrupted")
    tick(store)
    assert store.query(job["job_id"])["error"]["code"] == "ARTIFACT_VALIDATION_FAILED"
    assert not (directory / "public").exists()


def test_completed_file_loss_is_not_recomputed_or_rewritten_failed(store, job):
    directory = store.directory(job["job_id"])
    job.update(stage(store, job), publication_state="committing", commit_at=stamp())
    store.save(job)
    publish(store, job)
    (directory / "public/results.zip").unlink()
    with pytest.raises(JobError) as error:
        result(store, store.query(job["job_id"]))
    assert error.value.code == "ARTIFACT_UNAVAILABLE"
    assert store.load(job["job_id"])["status"] == "completed"


def test_expiry_cleanup_resumes_after_interrupted_deletion(store, job):
    directory = store.directory(job["job_id"])
    job.update(stage(store, job), publication_state="committing", commit_at=stamp())
    store.save(job)
    publish(store, job)
    job = store.load(job["job_id"])
    job["expires_at"] = stamp(time.time() - 1)
    store.save(job)
    real = shutil.rmtree
    called = []
    def interrupt(path, *args, **kwargs):
        if not called:
            called.append(path)
            real(path, *args, **kwargs)
            raise OSError("interrupted after removing public files")
        return real(path, *args, **kwargs)
    with patch("cascade_mcp.maintenance.shutil.rmtree", side_effect=interrupt):
        with pytest.raises(OSError):
            tick(store)
    tick(store)
    assert store.query(job["job_id"])["status"] == "expired"
    assert not (directory / "public").exists()


def test_manifest_never_resolves_private_paths(store, job):
    directory = store.directory(job["job_id"])
    prepared = stage(store, job)
    prepared["artifacts"][0]["name"] = "../job.json"
    assert not valid(directory / "staging", prepared["artifacts"])


def test_dispatch_recovery_tick_does_not_override_cleanup_interval(store, job):
    directory = store.directory(job["job_id"])
    job.update(stage(store, job), publication_state="committing", commit_at=stamp())
    store.save(job)
    publish(store, job)
    job = store.load(job["job_id"])
    job["expires_at"] = stamp(time.time() - 1)
    store.save(job)
    tick(store, cleanup=False)
    assert (directory / "public").exists()
    tick(store, cleanup=True)
    assert not (directory / "public").exists()
