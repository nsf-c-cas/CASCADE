import pytest
from fastmcp import Client
from cascade_mcp.jobs import JobError
from cascade_mcp.server import create_server, http_app
from cascade_mcp.service import Service
from test_jobs import confirmed


@pytest.mark.asyncio
async def test_four_tools_structured_success_error_and_failed_job_queries(store):
    service = Service(store, confirmed)
    server = create_server(store.cfg, service)
    async with Client(server) as client:
        assert {t.name for t in await client.list_tools()} == {"submit_analysis", "get_job_status", "get_job_result", "cancel_job"}
        result = await client.call_tool("submit_analysis", {"smiles": "CCO", "nuclei": ["Carbon"]})
        assert result.structured_content["ok"]
        job_id = result.structured_content["job_id"]
        invalid = await client.call_tool("get_job_status", {"job_id": "../bad"}, raise_on_error=False)
        assert invalid.is_error and invalid.structured_content["error"]["code"] == "INVALID_INPUT"
        job = store.load(job_id)
        store.finish(job, "failed", "INVALID_MOLECULE")
        query = await client.call_tool("get_job_result", {"job_id": job_id})
        assert not query.is_error and query.structured_content["status"] == "failed"


@pytest.mark.asyncio
async def test_host_origin_allowlists_and_endpoint_path(store, monkeypatch):
    import httpx
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://allowed.test")
    app = http_app(create_server(store.cfg, Service(store, confirmed)))
    async with app.lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/healthz", headers={"Origin": "https://allowed.test"})).status_code == 200
            assert (await client.get("/mcp", headers={"Origin": "https://evil.test"})).status_code == 403
            assert (await client.get("/healthz", headers={"Host": "evil.test"})).status_code == 400
