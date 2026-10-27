from concurrent.futures import ThreadPoolExecutor
import json
import time
from unittest.mock import patch
import pytest
from cascade_mcp.jobs import JobError, atomic, epoch, normalize, read, stamp
from cascade_mcp.maintenance import tick
from cascade_mcp.service import Service
from cascade_mcp.tasks import dispatch


def confirmed(store, record):
    with store.lock("state-" + record["job_id"]):
        record = store.load(record["job_id"])
        record["dispatch_state"] = "confirmed"
        store.save(record)
        return record


def test_input_contract():
    assert normalize(" CCO ", ["Carbon", "13C"], None, 100) == {"smiles": "CCO", "nuclei": ["13C"]}
    for values in (("", ["13C"], None), ("C", ["1H", "13C"], None), ("C", "13C", None),
                   ("C", ["bad"], None), ("C", ["1H"], "../path")):
        with pytest.raises(JobError) as error:
            normalize(*values, 100)
        assert error.value.code == "INVALID_INPUT"


def test_concurrent_retries_one_job_and_conflict(store):
    service = Service(store, confirmed)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: service.submit(" CCO ", ["Carbon"], "retry-key-123456789"), range(12)))
    assert len({r["job_id"] for r in values}) == 1
    assert len(store.ids()) == 1
    with pytest.raises(JobError) as error:
        service.submit("CCC", ["13C"], "retry-key-123456789")
    assert error.value.code == "REQUEST_ID_CONFLICT"


def test_reservation_recovers_same_id_after_creation_failure(store):
    create = store.save
    with patch.object(store, "save", side_effect=OSError("storage failure")):
        with pytest.raises(OSError):
            store.create({"smiles": "CCO", "nuclei": ["13C"]}, "retry-key-123456789")
    reserved = read(next((store.root / ".requests").iterdir()))
    record, reused = store.create(reserved["request"], "retry-key-123456789")
    assert reused and record["job_id"] == reserved["job_id"]


def test_uncertain_dispatch_has_same_id_and_no_fake_success(store, job):
    with patch("cascade_mcp.tasks.app.send_task", side_effect=ConnectionError("response lost")):
        with pytest.raises(JobError) as error:
            dispatch(store, job)
    assert error.value.code == "DISPATCH_UNCERTAIN"
    assert error.value.job_id == job["job_id"]
    assert store.load(job["job_id"])["dispatch_state"] == "uncertain"
    with patch("cascade_mcp.tasks.app.send_task") as send:
        result = dispatch(store, store.load(job["job_id"]))
        assert result["dispatch_state"] == "confirmed"
        assert send.call_args.kwargs["task_id"] == job["celery_task_id"]


def test_terminal_retries_never_dispatch_again(store, job):
    store.finish(job, "failed", "INVALID_MOLECULE")
    service = Service(store, lambda *_: pytest.fail("must not dispatch"))
    result = service.submit("CCO", ["13C"], "retry-key-123456789")
    assert result["job_id"] == job["job_id"] and result["status"] == "failed"
    assert service.result(job["job_id"])["ok"]


def test_queued_cancellation_prevents_execution_and_is_idempotent(store, job):
    first = store.cancel(job["job_id"])
    second = store.cancel(job["job_id"])
    assert first["cancel_accepted"] and first["status"] == "cancelled"
    assert not second["cancel_accepted"] and second["cancel_requested"]


def test_cancellation_after_completion_cutoff_is_unavailable_until_recovery(store, job):
    job["publication_state"] = "committing"
    store.save(job)
    for function in (store.cancel, store.query):
        with pytest.raises(JobError) as error:
            function(job["job_id"])
        assert error.value.code == "SERVICE_UNAVAILABLE"


def test_ids_private_paths_and_unknown_id(store):
    for value in ("../logs", "%2f", "x" * 42, "x/" * 22):
        with pytest.raises(JobError) as error:
            store.query(value)
        assert error.value.code == "INVALID_INPUT"
    with pytest.raises(JobError) as error:
        store.query("x" * 43)
    assert error.value.code == "JOB_NOT_FOUND"


def test_expiry_cleanup_records_keys_and_late_messages(store, job):
    store.finish(job, "completed")
    job["finished_at"], job["expires_at"] = stamp(time.time() - 15), stamp(time.time() - 5)
    store.save(job)
    assert store.query(job["job_id"])["status"] == "expired"
    assert store.cancel(job["job_id"])["status"] == "expired"
    tick(store)
    assert store.load(job["job_id"])["status"] == "expired"
    assert not (store.directory(job["job_id"]) / "input").exists()
    job = store.load(job["job_id"])
    job["finished_at"] = stamp(time.time() - 30)
    store.save(job)
    tick(store)
    assert not store.directory(job["job_id"]).exists()
    assert not list((store.root / ".requests").iterdir())
    from cascade_mcp.worker import execute_job
    execute_job(store.cfg, job["job_id"], "nonexistent-owner")
    assert not store.directory(job["job_id"]).exists()
    assert list((store.root / ".locks").glob("execution-*"))


def test_active_jobs_and_execution_locks_prevent_cleanup(store, job):
    job.update(status="running", owner="test-worker")
    store.save(job)
    tick(store)
    assert store.directory(job["job_id"]).exists()
    assert store.load(job["job_id"])["status"] == "running"


def test_dispatch_deadline_fails_uncertain_job_and_no_late_start(store, job):
    job.update(dispatch_state="uncertain", created_at=stamp(time.time() - 100))
    store.save(job)
    tick(store)
    assert store.query(job["job_id"])["error"]["code"] == "DISPATCH_FAILED"


def test_readiness_pause_and_queue_limits(store):
    atomic(store.root / ".admission-closed", {})
    with pytest.raises(JobError):
        Service(store, confirmed).submit("CC", ["1H"])
    (store.root / ".admission-closed").unlink()
    store.cfg.max_queue = 1
    Service(store, confirmed).submit("CC", ["1H"])
    with pytest.raises(JobError):
        Service(store, confirmed).submit("CC", ["1H"])


def test_cache_failure_does_not_override_durable_state(store, job):
    from unittest.mock import Mock
    store.cache = Mock()
    store.cache.setex.side_effect = ConnectionError("redis lost")
    store.finish(job, "cancelled")
    assert store.query(job["job_id"])["status"] == "cancelled"


def test_missing_executable_leaves_no_public_files_and_blocks_admission(store, job):
    from cascade_mcp.worker import execute_job
    with patch("cascade_mcp.worker.subprocess.Popen", side_effect=FileNotFoundError("missing executable")):
        execute_job(store.cfg, job["job_id"], "worker")
    assert store.query(job["job_id"])["error"]["code"] == "PREDICTION_FAILED"
    assert not (store.directory(job["job_id"]) / "public").exists()
    assert (store.root / ".blocked").exists()


def test_image_labels_do_not_block_dispatch_or_admission(store, job):
    store.cfg.worker_image = "cascade-worker:local"
    with patch("cascade_mcp.tasks.app.send_task") as send:
        dispatch(store, job)
        send.assert_called_once()
    assert Service(store, confirmed).submit("CCCC", ["13C"])["status"] == "queued"


def test_worker_restart_fails_unfinished_jobs_and_clears_old_pause(store, job):
    from cascade_mcp.worker import restart_jobs
    finished = store.create({"smiles": "CC", "nuclei": ["13C"]}, None)[0]
    store.finish(finished, "completed")
    atomic(store.root / ".admission-closed", {})
    restart_jobs(store)
    assert store.load(job["job_id"])["error"]["code"] == "WORKER_LOST"
    assert store.load(finished["job_id"])["status"] == "completed"
    assert not (store.root / ".admission-closed").exists()
    assert not (store.root / ".blocked").exists()


def test_retry_during_publication_returns_same_job_without_transient_error(store, job):
    job.update(status="running", publication_state="committing", dispatch_state="confirmed")
    store.save(job)
    response = Service(store, confirmed).submit("CCO", ["13C"], "retry-key-123456789")
    assert response["job_id"] == job["job_id"]
    assert response["status"] == "running"
    assert response["reused"]
