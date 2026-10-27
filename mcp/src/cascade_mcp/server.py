"""Anonymous Streamable HTTP MCP; all inference is dispatched asynchronously."""
import base64
import json
import os
import logging
import anyio
from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolResult, TextContent, ImageContent
from pydantic import StrictStr
import redis
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from cascade_mcp.jobs import JobError, Store
from cascade_mcp.service import Service
from cascade_mcp.settings import Settings


class Response(ToolResult):
    # Pinned FastMCP delegates this return value to the MCP SDK, preserving isError.
    def to_mcp_result(self):
        return CallToolResult(content=self.content, structuredContent=self.structured_content,
                              isError=not self.structured_content["ok"])


class Origins:
    def __init__(self, app, allowed):
        self.app, self.allowed = app, allowed

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [v.decode() for k, v in scope["headers"] if k.lower() == b"origin"]
            if values and (len(values) != 1 or values[0] not in self.allowed):
                return await JSONResponse({"error": "Invalid Origin"}, status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)


def create_server(cfg=None, service=None):
    cfg = cfg or Settings()
    store = service.store if service else Store(cfg, redis.Redis.from_url(cfg.backend, socket_timeout=2, socket_connect_timeout=2))
    service = service or Service(store)
    mcp = FastMCP("CASCADE", version="0.1.0", strict_input_validation=True,
                  instructions="Predict atom-level NMR chemical shifts for one candidate SMILES and one nucleus. Submit returns a job ID; poll later. No job listing or authentication. Keep job IDs and retry keys private.")

    async def invoke(function, *args, include_image=False):
        try:
            data = await anyio.to_thread.run_sync(function, *args)
            content = [TextContent(type="text", text=json.dumps(data, ensure_ascii=False))]
            if include_image and data["status"] == "completed":
                image = next(item for item in data["files"] if item["mime_type"] == "image/png")
                path = store.directory(data["job_id"]) / "public" / image["name"]
                if path.stat().st_size <= 1048576:
                    content.append(ImageContent(type="image", mimeType="image/png", data=base64.b64encode(path.read_bytes()).decode()))
        except JobError as error:
            data = error.response()
            content = [TextContent(type="text", text=json.dumps(data))]
        except Exception:
            logging.exception("CASCADE tool failure")
            data = JobError("SERVICE_UNAVAILABLE", retryable=True).response()
            content = [TextContent(type="text", text=json.dumps(data))]
        return Response(content=content, structured_content=data)

    @mcp.tool
    async def submit_analysis(smiles: StrictStr, nuclei: list[StrictStr], request_id: StrictStr | None = None) -> ToolResult:
        """Queue one SMILES prediction for ["1H"] or ["13C"] (aliases Hydrogen/Carbon). Returns immediately after dispatch confirmation, never waits for inference. Chemical validation can fail asynchronously. Persist an optional random request_id (16–128 URL-safe characters) before submission; reuse it after response loss to avoid duplicate computation. Retry keys are private, global, and retained for seven days after terminal confirmation."""
        return await invoke(service.submit, smiles, nuclei, request_id)

    @mcp.tool
    async def get_job_status(job_id: StrictStr) -> ToolResult:
        """Read status/stage and UTC timestamps using the private job ID; no computation or invented percentages/ETA."""
        return await invoke(service.status, job_id)

    @mcp.tool
    async def get_job_result(job_id: StrictStr, include_image: bool = True) -> ToolResult:
        """Read stored ppm summaries, partial atom-shift previews, provenance and public CSV/SDF/PNG/SVG/ZIP links. Optionally return PNG image content up to 1 MiB; rendering depends on the client. CASCADE predicts shifts for a candidate, not the correct structure or a complete spectrum. Expired/incomplete/failed jobs return their stored status without recomputation."""
        return await invoke(service.result, job_id, include_image=include_image)

    @mcp.tool
    async def cancel_job(job_id: StrictStr) -> ToolResult:
        """Request queued/running cancellation. cancel_accepted means new intent, not completed termination. Repeated requests and terminal jobs have no new side effects; publication commit cannot be cancelled."""
        return await invoke(service.cancel, job_id)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(_):
        try:
            store.ready()
            return JSONResponse({"ready": True})
        except JobError:
            return JSONResponse({"ready": False}, status_code=503)

    return mcp


def http_app(mcp):
    hosts = os.getenv("MCP_ALLOWED_HOSTS", "localhost,127.0.0.1,cascade-mcp").split(",")
    origins = os.getenv("MCP_ALLOWED_ORIGINS", "http://localhost").split(",")
    if any("*" in v for v in hosts + origins):
        raise ValueError("Explicit MCP Host/Origin allowlists are required")
    return mcp.http_app(path="/mcp", stateless_http=True, middleware=[
        Middleware(TrustedHostMiddleware, allowed_hosts=hosts), Middleware(Origins, allowed=origins)])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(http_app(create_server()), host="0.0.0.0", port=8000, proxy_headers=False)
