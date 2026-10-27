# CASCADE MCP: runtime and scientific details

Use [README.md](README.md) for commands and [DEPLOY_EC2.md](DEPLOY_EC2.md) for production.

## Deployment model

This is a research service: stopping/rebuilding containers may interrupt jobs.
Use direct Compose with stable tags `cascade-control:local` and `cascade-worker:local`.
There is no mandatory image-ID pinning, `.env.images`, manual image inspection,
legacy-image environment setting, build-order procedure or admission drain.

One `Dockerfile` provides `legacy`, `control` and `worker` targets. Compose builds
the required stages automatically. `scripts/deployment.py` is an internal build
helper: the metadata stage automatically hashes application/scripts, original model
source, model/preprocessor files and build inputs, then bakes `deployment.json`
into both remote images. It has no pin command. Git metadata is unavailable inside
the build context, so `source_revision` defaults to `unknown`; content hashes remain.
Image names in provenance are labels, not immutable image-ID attestations.

The worker's model/preprocessor preflight and the matching-build heartbeat remain;
they detect incomplete/mixed builds without requiring manually supplied image IDs.
After code changes, rebuild both images with `up -d --build`; after changing container
environment, use `up -d` rather than `restart`. A full `down`/`up` is acceptable.
Completed files and records live in the host bind mount; Redis uses a persistent AOF
volume. No container has the Docker socket or published MCP/Redis ports.

## Code and interpreter layout

- `src/cascade_mcp/`: MCP server, jobs, dispatch, supervisor, pipeline, artifacts, cleanup and optional administration.
- `scripts/`: standalone CLI/import check and internal build-metadata generator.
- `tests/`: unit/protocol/layout suites and opt-in real-inference Docker integration.

Containers use `PYTHONPATH=/app/src` and `python -m cascade_mcp.<module>`;
pytest config adds `src/`. Keep package initialization free of modern imports because
`legacy_adapter.py` is also imported/run with Python 3.5.

Control uses Python 3.12 and dependencies locked in `pyproject.toml`/`uv.lock`.
Scientific inference uses the separate `/opt/conda/bin/python` runtime: Python 3.5.6,
TensorFlow 1.12.0, Keras 2.2.4, RDKit 2018.03.3 and bundled `nfp`. Keras 2.2.5 breaks
the bundled `activations` import. Do not upgrade dependencies or alter upstream
science/models as part of deployment work. Legacy requirements are not a complete
transitive lock. ARM Macs use amd64 emulation; measured local success is not an EC2
performance guarantee. Warning filters suppress only known legacy import notices.

The CLI and adapter set the original models working directory/PYTHONPATH; upstream
loads files by relative paths. Both remote runtimes are in the worker container;
modern code invokes the legacy interpreter with fixed argument lists, not a shell.

## Input and output

Submit one candidate SMILES and one nucleus: `["1H"]` or `["13C"]`; case-insensitive
Hydrogen/Carbon aliases normalize to those values. Scalars and mixed nuclei are
rejected. Legacy chemistry validation requires one connected neutral fragment,
C/H/N/O/F/P/S/Cl, the configured total-atom limit including hydrogens, and atoms of
the requested nucleus. Chemistry errors may be asynchronous.

A private random `request_id` (16–128 URL-safe characters) supports retries: identical
normalized input returns the same job, including failed/cancelled/expired jobs.
Different input conflicts; recomputation requires a new key. `DISPATCH_UNCERTAIN`
returns the existing job ID; preserve the original key after response loss.

Results provide ppm summaries, partial atom previews, provenance and per-nucleus
CSV/SDF/PNG/SVG plus ZIP links. `get_job_result` includes actual PNG image content
up to 1 MiB by default; `include_image=false` returns metadata/links only. SVG is a
download. Client image rendering is separate from delivery and remains unverified
in named public clients. No custom MCP Apps viewer is implemented.

Preserve one-based upstream atom IDs across drawings/CSV/SDF; match conformers by
`(mol_id, cf_id)` rather than SDF record order. Explicit hydrogens remain visible.
`weighted_shift.csv` contains weighted shifts; `conformers_shift.csv` contains
per-conformer predictions. A 2D annotation is not a spectrum or proof that a candidate
structure is correct. Conformer counts can vary. Keep MIT notices and cite Guan et al.,
*Chem. Sci.* **2021**, DOI: 10.1039/D1SC03343C.

## Jobs, interruption and storage

`job.json` is authoritative; Redis is recoverable queue/cache. Atomic replacement and
stable `.locks/` files protect state. Worker-local supervision owns child cleanup;
maintenance never infers process death from its own PID namespace. The execution-lock
FD reaches pipeline/legacy children, preventing concurrent execution after parent loss.

Lock allocation is bounded: retry, execution, state and dispatch each use 256 hashed
stripes, plus two global locks (at most 1,026 new files). Unknown job IDs and rejected
submissions cannot allocate an unlimited number of locks. Separate pools preserve
lock ordering; collisions serialize unrelated keys without merging their job records.
Before upgrading from per-key locks, stop the entire application with Compose `down`,
then rebuild/start. Do not mix old/new processes. Old per-key files are retained;
offline cleanup is optional and requires all application processes to be stopped.

Every worker startup marks old queued/running/cancelling jobs failed with `WORKER_LOST`;
it does not resume computation. New requests work after startup recovery/preflight.
Completed results survive restart. A validated publication already at its durable
commit point may finish publication during recovery. Optional admin pause flags are
cleared on successful worker restart; pause/resume is not part of normal deployment.

Cancellation records intent, terminates running children, then confirms cancellation.
A durable `publication_state=committing` plus manifest is the cancellation cutoff;
only validated files are atomically moved into `public/`. Private inputs/logs/records
are never exposed by Nginx. Corrupt state still requires investigation rather than
fabricated success. Missing completed files return `ARTIFACT_UNAVAILABLE` without recomputation.

Defaults: one Celery solo slot, 100 total atoms, 20 active jobs, 3600-second full
pipeline timeout, 10-second termination grace, two worker CPUs and 4 GiB memory.
These are operator settings, not scientific applicability limits. Artifacts/logs
last 24 hours; records/retry keys seven days after terminal confirmation. Static-file
cleanup can lag expiry by the maintenance interval (default five minutes). Stable
locks remain online; only prune them with all application processes stopped.

## Validation boundaries

Run the suites from README. Docker tests use isolated Compose projects, Redis volumes,
network, storage and Nginx; they cover real predictions/downloads/images, cancellation,
timeout, process loss, hard worker restart, completed-result preservation, Redis/MCP
restart, expiry and multi-conformer mapping. Unit tests include retries, interrupted
publication/cleanup, source-layout metadata, readiness and Host/Origin validation.

Local validation on 2026-10-22: **37 tests passed** (30 unit/protocol/layout and
seven Docker integration tests). Standalone import check also printed `IMPORT OK`.

Actual ngrok/client acceptance, public DNS/TLS/ACME, Ubuntu EC2 resource sizing,
storage and reboot acceptance remain separate from local Docker validation.
