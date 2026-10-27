# CASCADE MCP: run commands

Run from `CASCADE/mcp/`. Requires Docker with Compose v2; application images target
`linux/amd64`. Source: `src/cascade_mcp/`; utilities: `scripts/`; tests: `tests/`.

- [EC2 deployment](DEPLOY_EC2.md)
- [Runtime and scientific details](README_details.md)
- [Shared Nginx setup](../../mcp-infra/README.md)

## 1. Configure

```bash
cp -n .env.local.example .env.local
```

Edit `.env.local`: `PROJECT_ROOT` is the absolute parent of `CASCADE/` and
`mcp-infra/`; set the actual ngrok URL and hostname allowlists:

```dotenv
PUBLIC_BASE_URL=https://YOUR_NGROK_DOMAIN/cascade
MCP_ALLOWED_HOSTS=localhost,127.0.0.1,cascade-mcp,YOUR_NGROK_DOMAIN
MCP_ALLOWED_ORIGINS=https://YOUR_NGROK_DOMAIN
```

Configure/start [mcp-infra](../../mcp-infra/README.md) first. It creates `mcp-proxy`
and `${PROJECT_ROOT}/mcp-results/cascade`.

## 2. Build and start

```bash
docker compose --env-file .env.local -f compose.mcp.yml up -d --build
docker compose --env-file .env.local -f compose.mcp.yml ps
docker compose --env-file .env.local -f compose.mcp.yml logs --tail 100 cascade-worker
```

Or build separately: `docker compose --env-file .env.local -f compose.mcp.yml build`.
One multi-stage `Dockerfile` builds both runtimes and generates provenance automatically.
No image inspection, manual manifest command, pinning, or `.env.images` is needed.
Old `CASCADE_LEGACY_IMAGE`/image-ID settings are ignored; stop passing `.env.images`.

Wait for `cascade-mcp` to become healthy. Register
`https://YOUR_NGROK_DOMAIN/cascade/mcp` in the MCP client. Note that `Authentication` option should be set to `No Authentication`, not `OAuth`. Application ports remain private.
Tools: `submit_analysis`, `get_job_status`, `get_job_result`, `cancel_job`.
Submit one SMILES with `nuclei=["1H"]` or `["13C"]`; preserve `request_id` across retries.

## 3. Update, restart or stop

```bash
docker compose --env-file .env.local -f compose.mcp.yml down
docker compose --env-file .env.local -f compose.mcp.yml up -d --build
```

For unchanged code, `restart` is enough:

```bash
docker compose --env-file .env.local -f compose.mcp.yml restart
```

No pause/drain procedure is required. Worker restart fails unfinished jobs with
`WORKER_LOST`; completed results remain until expiry. Submit a new `request_id` to
retry a failed calculation. Do not add `-v` to routine shutdown; it deletes Redis data.

For the bounded-lock security update, run `down` before `up -d --build` as above:
old and new application processes must not run together. Existing lock files are
retained; remove obsolete locks only with all application containers stopped.

## Tests

Requires uv and Python 3.12; Docker tests require built images and sibling `mcp-infra/`.

```bash
uv sync
uv run pytest -q
RUN_CASCADE_DOCKER_TESTS=1 uv run pytest -q
```

## Standalone CLI (no MCP server)

```bash
docker compose -f compose.yml build cascade
docker compose -f compose.yml run --rm cascade
docker compose -f compose.yml run --rm cascade python /opt/cascade-test/predict_2d.py \
  --smiles 'CC1=CC(=CC(=C1)O)C' --nuclei 1H 13C --output /output/example
```

The first run prints `IMPORT OK`; prediction outputs go to `results/example/`.
Reusing the path overwrites files. Annotated structures are not NMR spectra.
Preserve upstream MIT notices; cite Guan et al., *Chem. Sci.* **2021**, DOI: 10.1039/D1SC03343C.

## Example prompts for AI clients

After connecting CASCADE to your AI service (such as ChatGPT), try these prompts.
Image display depends on the client; download links remain available.

```text
Use CASCADE to predict 1H chemical shifts for SMILES CC1=CC(=CC(=C1)O)C.
Submit one job with nuclei=["1H"]. Generate and retain a random request_id;
reuse it if submission must be retried. Poll the returned job_id until terminal.
If completed, fetch get_job_result with include_image=true, show the annotated
2D structure if supported, summarize the predicted shifts in ppm, and provide
PNG, SVG, CSV, SDF and ZIP download links. If it fails, report the actual error.
```

or simply:

```text
Use CASCADE with SMILES CC1=CC(=CC(=C1)O)C and Carbon
```
