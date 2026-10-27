"""Lock allocation stays bounded even when public requests are rejected."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cascade_mcp import jobs
from cascade_mcp.jobs import JobError
from cascade_mcp.maintenance import tick
from cascade_mcp.service import Service


def test_unknown_jobs_and_rejected_requests_cannot_grow_locks_unboundedly(store):
    (store.root / ".worker.json").unlink()
    service = Service(store)
    for number in range(1200):
        job_id = str(number).zfill(43)
        for method in (service.status, service.result, service.cancel):
            with pytest.raises(JobError) as error:
                method(job_id)
            assert error.value.code == "JOB_NOT_FOUND"
        with pytest.raises(JobError) as error:
            service.submit("CCO", ["13C"], "security-retry-" + str(number).zfill(16))
        assert error.value.code == "SERVICE_UNAVAILABLE"
    tick(store)
    assert store.ids() == []
    assert not list((store.root / ".requests").iterdir())
    assert len(list((store.root / ".locks").iterdir())) <= 1026  # Four pools of 256 plus two global locks.


def test_colliding_keys_serialize_but_lock_types_do_not_collide(store, monkeypatch):
    monkeypatch.setattr(jobs, "LOCK_STRIPES", 1)
    with store.lock("retry-first"):
        with store.lock("retry-second", blocking=False) as same_pool:
            assert same_pool is None
        with store.lock("execution-second", blocking=False) as execution:
            assert execution is not None
            with store.lock("state-second", blocking=False) as state:
                assert state is not None
                with store.lock("dispatch-second", blocking=False) as dispatch:
                    assert dispatch is not None


def test_lock_exclusion_across_independent_processes(store):
    code = """
import sys
from pathlib import Path
from cascade_mcp.jobs import Store
from cascade_mcp.settings import Settings
store = Store(Settings(jobs=Path(sys.argv[1]), manifest={}))
with store.lock('execution-process-test', blocking=False) as fd:
    print('acquired' if fd is not None else 'blocked')
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
               PYTHONDONTWRITEBYTECODE="1")
    def child():
        return subprocess.check_output([sys.executable, "-c", code, str(store.root)], env=env, text=True).strip()
    with store.lock("execution-process-test"):
        assert child() == "blocked"
    assert child() == "acquired"


def test_distinct_retry_keys_remain_independent_when_stripes_collide(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(jobs, "LOCK_STRIPES", 1)
    request = {"smiles": "CCO", "nuclei": ["13C"]}
    keys = ["collision-retry-" + str(n).zfill(16) for n in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda key: store.create(request, key)[0]["job_id"], keys, timeout=10))
    assert len(set(ids)) == len(keys)
    assert [store.create(request, key)[0]["job_id"] for key in keys] == ids


@pytest.mark.parametrize("key", ["unknown-1", "state", "admission-1", "worker-1", "../state"])
def test_unknown_lock_namespaces_do_not_allocate_files(store, key):
    with pytest.raises(JobError) as error:
        with store.lock(key):
            pytest.fail("Unknown lock namespace was accepted")
    assert error.value.code == "INVALID_INPUT"
    assert not list((store.root / ".locks").iterdir())
