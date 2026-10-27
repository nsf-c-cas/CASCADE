"""Opt-in real inference through Nginx/MCP/Redis/Celery; isolated Compose projects."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import uuid
import httpx
import pytest
from fastmcp import Client

pytestmark = pytest.mark.skipif(os.getenv("RUN_CASCADE_DOCKER_TESTS") != "1", reason="Opt-in real Docker/inference suite")
ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT.parent / "mcp-infra"
SMILES = "CC1=CC(=CC(=C1)O)C"


def command(args, capture=False):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else None)


class Stack:
    def __init__(self, directory):
        self.directory = directory
        self.name = "cascade-test-" + uuid.uuid4().hex[:8]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = "http://localhost:%s/cascade/mcp" % self.port
        self.env = directory / "application.env"
        values = {"PROJECT_ROOT": str(directory), "COMPOSE_PROJECT_NAME": self.name,
                  "MCP_PROXY_NETWORK": self.name, "PUBLIC_BASE_URL": "http://localhost:%s/cascade" % self.port,
                  "RESULT_RETENTION_SECONDS": "10", "RECORD_RETENTION_SECONDS": "30",
                  "CLEANUP_INTERVAL_SECONDS": "1", "CANCEL_GRACE_SECONDS": "1"}
        self.values = values
        self.write_env()
        spec = importlib.util.spec_from_file_location("infra_test", INFRA / "scripts/infra.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.infra = module
        self.infra_env = directory / "infra.env"
        self.infra_env.write_text("\n".join(["PROJECT_ROOT=" + str(directory), "INFRA_STATE_DIR=" + str(directory / "infra-state"),
            "PUBLIC_HOST=localhost", "HTTP_PORT=" + str(self.port), "COMPOSE_PROJECT_NAME=" + self.name + "-infra",
            "MCP_PROXY_NETWORK=" + self.name]) + "\n")
        self.config = module.Config(self.infra_env, "local")

    def write_env(self):
        self.env.write_text("\n".join(k + "=" + v for k, v in self.values.items()) + "\n")

    def compose(self, *args, capture=False):
        return command(["docker", "compose", "--env-file", str(self.env), "-f", str(ROOT / "mcp/compose.mcp.yml"), *args], capture)

    def proxy(self, *args):
        return command(["docker", "compose", "--env-file", str(self.infra_env), "-f", str(INFRA / "docker-compose.yml"),
                        "-f", str(INFRA / "docker-compose.local.yml"), *args])

    async def ready(self):
        for _ in range(180):
            try:
                async with Client(self.url) as client:
                    result = await client.list_tools()
                    if len(result) == 4:
                        # Tool discovery alone is not worker readiness.
                        inside = self.compose("exec", "-T", "cascade-mcp", "/opt/control/bin/python", "-c",
                            "from cascade_mcp.settings import Settings; from cascade_mcp.jobs import Store; Store(Settings()).ready()", capture=True)
                        return
            except Exception:
                await asyncio.sleep(0.5)
        raise AssertionError("MCP/worker did not become ready")

    async def wait(self, client, job_id, states, timeout=150):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            data = (await client.call_tool("get_job_status", {"job_id": job_id})).structured_content
            if data["status"] in states:
                return data
            await asyncio.sleep(0.2)
        raise AssertionError("Job did not reach " + str(states))


@pytest.fixture(scope="module")
def stack():
    with tempfile.TemporaryDirectory(prefix="cascade-integration-", dir="/tmp") as folder:
        stack = Stack(Path(folder).resolve())
        try:
            stack.infra.configure(stack.config)
            stack.compose("up", "-d", "--no-build")
            stack.proxy("up", "-d", "nginx")
            yield stack
        finally:
            try:
                stack.compose("logs", "--no-color", "--tail", "100")
            finally:
                stack.proxy("down")
                stack.compose("down", "-v")  # Only this test project's disposable Redis volume.
                subprocess.run(["docker", "network", "rm", stack.name], check=False)


@pytest.mark.asyncio
async def test_01_real_nuclei_summaries_images_downloads_and_retries(stack):
    await stack.ready()
    async with Client(stack.url) as client:
        for nucleus, count in (("1H", 10), ("13C", 8)):
            key = uuid.uuid4().hex
            response = await client.call_tool("submit_analysis", {"smiles": SMILES, "nuclei": [nucleus], "request_id": key})
            job_id = response.structured_content["job_id"]
            repeated = await client.call_tool("submit_analysis", {"smiles": SMILES, "nuclei": [nucleus], "request_id": key})
            assert repeated.structured_content["job_id"] == job_id
            state = await stack.wait(client, job_id, {"completed", "failed"})
            assert state["status"] == "completed", state
            result = await client.call_tool("get_job_result", {"job_id": job_id})
            data = result.structured_content
            assert data["results"][nucleus]["atom_count"] == count
            assert any(content.type == "image" for content in result.content)
            async with httpx.AsyncClient() as http:
                for file in data["files"]:
                    download = await http.get(file["url"])
                    assert download.status_code == 200, file
                    assert download.content
                private = await http.get("http://localhost:%s/cascade/results/%s/job.json" % (stack.port, job_id))
                assert private.status_code == 404


@pytest.mark.asyncio
async def test_02_invalid_queued_and_running_cancellation(stack):
    async with Client(stack.url) as client:
        bad = await client.call_tool("submit_analysis", {"smiles": "C.C", "nuclei": ["13C"]})
        state = await stack.wait(client, bad.structured_content["job_id"], {"failed"})
        assert state["error"]["code"] == "INVALID_MOLECULE"
        first = (await client.call_tool("submit_analysis", {"smiles": "CCCCO", "nuclei": ["13C"]})).structured_content["job_id"]
        await stack.wait(client, first, {"running"})
        second = (await client.call_tool("submit_analysis", {"smiles": SMILES, "nuclei": ["1H"]})).structured_content["job_id"]
        queued = (await client.call_tool("cancel_job", {"job_id": second})).structured_content
        assert queued["status"] == "cancelled"
        await client.call_tool("cancel_job", {"job_id": first})
        assert (await stack.wait(client, first, {"cancelled"}))["status"] == "cancelled"
        assert not (stack.directory / "mcp-results/cascade" / first / "public").exists()


@pytest.mark.asyncio
async def test_03_worker_execution_process_loss_cleanup(stack):
    async with Client(stack.url) as client:
        job_id = (await client.call_tool("submit_analysis", {"smiles": "CCCCO", "nuclei": ["13C"]})).structured_content["job_id"]
        await stack.wait(client, job_id, {"running"})
        stack.compose("exec", "-T", "cascade-worker", "/opt/control/bin/python", "-c",
                      "import os,json,signal; os.kill(json.load(open('/data/jobs/.worker.json'))['celery_pid'],signal.SIGKILL)")
        state = await stack.wait(client, job_id, {"failed"})
        assert state["error"]["code"] == "WORKER_LOST"
    await stack.ready()


@pytest.mark.asyncio
async def test_04_actual_execution_timeout(stack):
    stack.values["MAX_RUNTIME_SECONDS"] = "1"
    stack.write_env()
    stack.compose("up", "-d", "--no-build", "cascade-worker")
    await stack.ready()
    async with Client(stack.url) as client:
        job_id = (await client.call_tool("submit_analysis", {"smiles": SMILES, "nuclei": ["13C"]})).structured_content["job_id"]
        state = await stack.wait(client, job_id, {"failed"})
        assert state["error"]["code"] == "TIMEOUT"
    stack.values.pop("MAX_RUNTIME_SECONDS")
    stack.write_env()
    stack.compose("up", "-d", "--no-build", "cascade-worker")
    await stack.ready()


@pytest.mark.asyncio
async def test_05_mcp_redis_restart_does_not_kill_prediction_and_expiry(stack):
    async with Client(stack.url) as client:
        job_id = (await client.call_tool("submit_analysis", {"smiles": SMILES, "nuclei": ["13C"]})).structured_content["job_id"]
        await stack.wait(client, job_id, {"running"})
    stack.compose("restart", "cascade-mcp", "cascade-redis")
    await stack.ready()
    async with Client(stack.url) as client:
        state = await stack.wait(client, job_id, {"completed", "failed"})
        assert state["status"] == "completed", state
        result = (await client.call_tool("get_job_result", {"job_id": job_id, "include_image": False})).structured_content
        link = result["files"][0]["url"]
        await stack.wait(client, job_id, {"expired"}, timeout=30)
        expired = (await client.call_tool("get_job_result", {"job_id": job_id})).structured_content
        assert expired["files"] == []
        async with httpx.AsyncClient() as http:
            for _ in range(30):
                if (await http.get(link)).status_code == 404:
                    break
                await asyncio.sleep(0.2)
            assert (await http.get(link)).status_code == 404


def test_06_flexible_fixture_mapping_uses_ids_not_sdf_order(stack):
    import shutil
    fixtures = Path(__file__).parent / "fixtures/flexible"
    output = stack.directory / "flexible/13C"
    shutil.copytree(fixtures, output)
    sdf = output / "conformers.sdf"
    records = [r for r in sdf.read_text().split("$$$$\n") if r.strip()]
    assert len(records) > 1
    sdf.write_text("$$$$\n".join(reversed(records)) + "$$$$\n")
    image = "cascade-worker:local"
    code = "import json,sys; sys.path.insert(0,'/app/src'); from cascade_mcp.legacy_adapter import validate_results; print(json.dumps(validate_results('/fixture','13C')))"
    result = command(["docker", "run", "--rm", "--platform", "linux/amd64", "--entrypoint", "/opt/conda/bin/python",
                      "--mount", "type=bind,src=" + str(output.parent) + ",dst=/fixture,readonly", image, "-c", code], True)
    assert json.loads(result.stdout)["conformer_count"] == len(records)
    sdf.write_text(records[0] + "$$$$\n")
    with pytest.raises(subprocess.CalledProcessError):
        command(["docker", "run", "--rm", "--platform", "linux/amd64", "--entrypoint", "/opt/conda/bin/python",
                 "--mount", "type=bind,src=" + str(output.parent) + ",dst=/fixture,readonly", image, "-c", code], True)


@pytest.mark.asyncio
async def test_07_hard_worker_restart_fails_pending_jobs_preserves_completed_results(stack):
    stack.values.update(RESULT_RETENTION_SECONDS="120", RECORD_RETENTION_SECONDS="240")
    stack.write_env()
    stack.compose("up", "-d", "--no-build")
    await stack.ready()
    async with Client(stack.url) as client:
        completed = (await client.call_tool("submit_analysis", {"smiles": SMILES, "nuclei": ["13C"]})).structured_content["job_id"]
        assert (await stack.wait(client, completed, {"completed", "failed"}))["status"] == "completed"
        result = (await client.call_tool("get_job_result", {"job_id": completed})).structured_content
        running = (await client.call_tool("submit_analysis", {"smiles": "CCCCO", "nuclei": ["13C"]})).structured_content["job_id"]
        await stack.wait(client, running, {"running"})
        queued = (await client.call_tool("submit_analysis", {"smiles": SMILES, "nuclei": ["1H"]})).structured_content["job_id"]
    stack.compose("stop", "-t", "0", "cascade-worker")
    stack.compose("up", "-d", "--no-build", "cascade-worker")
    await stack.ready()
    async with Client(stack.url) as client:
        for job_id in (running, queued):
            state = await stack.wait(client, job_id, {"failed"})
            assert state["error"]["code"] == "WORKER_LOST"
        assert (await client.call_tool("get_job_status", {"job_id": completed})).structured_content["status"] == "completed"
        async with httpx.AsyncClient() as http:
            assert (await http.get(result["files"][0]["url"])).status_code == 200
        fresh = (await client.call_tool("submit_analysis", {"smiles": SMILES, "nuclei": ["13C"]})).structured_content["job_id"]
        assert (await stack.wait(client, fresh, {"completed", "failed"}))["status"] == "completed"


@pytest.mark.asyncio
async def test_08_public_unknown_job_and_rejected_submit_use_bounded_locks(stack):
    await stack.ready()
    before = {p.name for p in (stack.directory / "mcp-results/cascade").iterdir() if not p.name.startswith(".")}
    stack.compose("exec", "-T", "cascade-mcp", "/opt/control/bin/python", "-m", "cascade_mcp.admin", "pause")
    try:
        async with Client(stack.url) as client:
            for number in range(32):
                for tool in ("get_job_status", "get_job_result", "cancel_job"):
                    result = await client.call_tool(tool, {"job_id": str(number).zfill(43)}, raise_on_error=False)
                    assert result.structured_content["error"]["code"] == "JOB_NOT_FOUND"
                result = await client.call_tool("submit_analysis", {"smiles": "CCO", "nuclei": ["13C"],
                    "request_id": "security-retry-" + str(number).zfill(16)}, raise_on_error=False)
                assert result.structured_content["error"]["code"] == "SERVICE_UNAVAILABLE"
    finally:
        stack.compose("exec", "-T", "cascade-mcp", "/opt/control/bin/python", "-m", "cascade_mcp.admin", "resume")
    root = stack.directory / "mcp-results/cascade"
    assert {p.name for p in root.iterdir() if not p.name.startswith(".")} == before
    locks = list((root / ".locks").iterdir())
    assert len(locks) <= 1026
    for path in locks:
        if path.name not in {"admission", "worker"}:
            kind, _, stripe = path.name.partition("-")
            assert kind in {"retry", "state", "execution", "dispatch"}
            assert stripe.isdigit() and 0 <= int(stripe) < 256
