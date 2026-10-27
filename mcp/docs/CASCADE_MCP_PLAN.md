# Plan for Providing CASCADE as the cascade MCP Server

Deployment alignment: CASCADE uses deployment identifier `cascade`, while its checkout remains `CASCADE/`. See [mcp-infra README](mcp-infra/README.md) for implemented infrastructure commands and [mcp_infra_plan.md](mcp_infra_plan.md) for the consolidated plan. General Compose snippets below remain design examples; use the implemented remote CASCADE Compose file and its README.

Created: 2026-10-21
Review incorporated: clarified job records and queue-dispatch recovery, submission retries, conformer ID mapping, automatic build provenance, and tool response contracts.
Final path review: added rules for startup/cancellation races, process ownership and lock lifetimes, failures at each stage, and interrupted expiry cleanup.
Status: local implementation added in CASCADE/mcp; actual public-client and EC2 deployment acceptance remains pending. Use CASCADE/mcp/README.md for direct Compose build/start commands.
Parent plan: [Multi-Research MCP Server Prototype Plan](multi_mcp_server_prototype_plan.md), updated 2026-10-21.


## 1. Purpose and Scope

Use the CASCADE repository as `cascade` in the parent plan. When the shared `mcp-infra` is built in another project, use this document as the CASCADE implementation specification. `cascade` is the deployment name; CASCADE is the actual prediction program.

Follow the parent document for shared infrastructure, while applying the CASCADE-specific rules below for authoritative job records, recovery, and deployment order. Do not omit these rules by copying only the parent document's general examples.

The first implementation accepts a single SMILES string and `1H` or `13C` alone (using both at once are not allowed), and provides asynchronous prediction, status queries, cancellation, result summaries, 2D images, and individual file/ZIP downloads. Follow the parent plan's decisions to use no authentication, retain results for 24 hours after completion, and use Streamable HTTP.

Preserve CASCADE's original computation code, models, and datasets. Place new execution-management code in `mcp/`. Restoring the Django web server, Gaussian calculations, model retraining, XYZ input, batch processing of multiple molecules, and external website automation are outside the first implementation's scope.

An external 3D viewer is a later stage. The first version provides SDF downloads containing 3D coordinates; do not return links to a viewer that does not yet exist. An interactive 3D UI inside chat is also a separate stage.

## 2. Existing Execution Baseline

The preserved baseline described here is the standalone execution environment. The remote MCP configuration is now separate in `mcp/compose.mcp.yml`.

- `mcp/compose.yml`: Compose project `cascade-standalone`, running a one-off `cascade` container.
- `mcp/Dockerfile`: Linux `amd64`, Python 3.5, TensorFlow 1.12.0, Keras 2.2.4, and RDKit 2018.03.03.
- `mcp/scripts/predict_2d.py`: CLI that calls the original prediction functions and generates PNG, SVG, CSV, and SDF files.
- `mcp/scripts/check_environment.py`: checks dependency imports and imports of the original prediction functions.
- `mcp/README.md`, `mcp/AGENTS.md`: execution instructions and compatibility constraints.

The following example has been validated through loading both models, actual prediction, and output-file generation. Reading the generated files, atom-index mapping, and PNG display were also checked.

```bash
cd ./CASCADE/mcp
docker compose run --rm cascade python /opt/cascade-test/predict_2d.py \
  --smiles 'CC1=CC(=CC(=C1)O)C' \
  --nuclei 1H \
  --output /output/example
```

At validation time, the runs produced 10 proton predictions and 8 carbon predictions, with one retained conformer in each run. This does not validate executability, accuracy, or runtime for other molecules, or exact numerical equivalence with the website.

Keras 2.2.4 matches both subprojects' `webapp_env.yml` files and the `keras_version` metadata in both SMILES models. Distinguish this from 2.2.5 in the top-level `environment.yml`; retain 2.2.4 for the MCP configuration.

## 3. CASCADE's Placement Within the Parent Plan

Follow the parent plan's host layout. `/project_root` is a placeholder for an actual absolute path, not a single Git repository.

```text
/project_root/
  mcp-infra/             Separate shared infrastructure repository: Nginx and Certbot
  CASCADE/                   Checkout root of this CASCADE repository
    mcp/                 CASCADE-specific MCP, worker, maintenance, and deployment files
  bbb/                   Independent analysis repository to be added later
  mcp-results/
    cascade/                 Persistent CASCADE job/result storage, outside Git
    bbb/                 Storage dedicated to bbb
```

Assume CASCADE's `README.md`, `mcp/`, and original subprojects are directly under `/project_root/CASCADE`. An additional `CASCADE/CASCADE/` level is not required. The current local checkout is `./CASCADE`.

The existing `mcp/results/` contains standalone test results. Use `/project_root/mcp-results/cascade` for the remote configuration; do not migrate the existing example results automatically.

Keep the following services and responsibilities:

- `cascade-mcp`: FastMCP on modern Python. Validates input format, submits and queries jobs, and records cancellation requests. Serves internally at `0.0.0.0:8000/mcp`.
- `cascade-redis`: Celery queue and small state/metadata storage dedicated to CASCADE. Do not share it with other analysis projects.
- `cascade-worker`: includes modern Python/Celery and the legacy CASCADE runtime. Launches a separate computation process for each job.
- `cascade-maintenance`: cleans up expired results and job records on modern Python, independently of long predictions.
- `mcp-infra/nginx`: public HTTPS, MCP proxying, and serving allowed result files.
- `mcp-infra/certbot`: certificate issuance and renewal from the parent plan. Do not duplicate it in the CASCADE repository.

Connect only Nginx and `cascade-mcp` to the shared `mcp-proxy` network. Keep `cascade-redis`, `cascade-worker`, and `cascade-maintenance` on cascade's internal network. Do not expose MCP or Redis ports directly on the host.

## 4. Separating Python Versions and Extending Images

The recommended approach is **two isolated Python environments inside one worker container**. This preserves the parent plan's service count and lets the worker directly manage the computation processes it must cancel.

- Control environment: select FastMCP, Celery, and state-management packages using Python 3.12 as the baseline; manage and pin dependencies with uv.
- Computation environment: retain the validated Python 3.5, TensorFlow, Keras, and RDKit environment. Do not use uv for it.
- The control environment does not import CASCADE, Keras, or TensorFlow directly. Launch legacy Python by absolute path through subprocess.
- MCP and maintenance images contain only the modern environment. Only the worker image reuses/extends the existing computation image's environment.

Example interpreter paths are `/opt/control/bin/python` for the modern environment and `/opt/conda/bin/python` for the existing computation environment. the `control` and `worker` targets in `mcp/Dockerfile` now provide these separate runtimes; the local two-runtime gate ran both example nuclei successfully. Modern dependencies are locked in `uv.lock`.

**First acceptance gate:** confirm distinct Python versions and executable paths, and run the existing CLI from the modern environment to obtain the same types of results as the standalone validation. Also check environment variables and library search paths so packages from one environment do not leak into the other.

The modern worker uses `Popen` to start the computation process inside the same container. Do not invoke `docker compose run` or `docker exec` from inside the worker, or mount the Docker socket. A normal subprocess cannot execute a Python interpreter located in another container.

Keep the existing `mcp/Dockerfile` and `mcp/compose.yml` for standalone regression validation. Add the remote configuration separately as `mcp/compose.mcp.yml` with dedicated image-build settings. Explicitly name the Compose project `cascade` to distinguish it from `cascade-standalone`. Adjust the parent document's `docker compose up` examples to specify `-f` and env-file paths for this layout.

For the remote worker, copy original source/models into `/src` at build time. One multi-stage `mcp/Dockerfile` supplies legacy/control/worker targets; Compose builds them automatically with stable tags. Metadata is generated during the build, with source/model hashes and runtime information; Git revision defaults to unknown when unavailable. No manual image inspection, digest pinning, `.env.images`, `CASCADE_LEGACY_IMAGE`, or manifest/pin command is required.

Research operations may stop/rebuild anytime using direct Compose. Mandatory admission draining is removed. On worker startup, unfinished queued/running/cancelling jobs fail with `WORKER_LOST`; completed results remain, and validated committed publication may recover. Retry failed calculations with a new request ID. Matching-build heartbeat, model preflight, private-file protection and atomic publication remain.

Shared Nginx configuration, TLS, and ngrok execution belong in `mcp-infra`. CASCADE supplies only the service names, ports, shared paths, and public URL contract.

## 5. MCP Tools

Use the same four tools as the parent plan. Tool descriptions must state CASCADE's role, inputs, and units. Here, terminal means the first confirmed `completed`, `failed`, or `cancelled` state; a later transition to `expired` does not restart the record or retry-key retention period.

### 5.1 submit_analysis

Example input contract:

```json
{
  "smiles": "CC1=CC(=CC(=C1)O)C",
  "nuclei": ["1H"],
  "request_id": "f644df74-9108-4ac2-8ce1-3bcae899c7c0"
}
```

- Accept one SMILES string and a nonempty `nuclei` list.
- Allow only `1H` alone or `13C` alone. Using both `1H` and `13C` at once are not allowed.
- Accept `Hydrogen`/`Carbon` aliases in the one-element list and normalize them to `1H`/`13C` before retry-key comparison.
- Do not accept user-supplied output paths, model paths, commands, or arbitrary CLI arguments.
- The MCP layer validates types, empty values, length limits, and nucleus selection. Molecular parsing, elements, charges, and atom-count limits are checked in the legacy environment before computation. Installing a separate RDKit in the modern MCP environment is not the default approach.
- A SMILES string that passes format validation can therefore still produce a `failed` job during chemical validation. State this in the tool description.
- Follow the current CLI's scope: each atom has formal charge zero, and elements are C, H, N, O, F, P, S, and Cl. Add validation in the execution adapter to reject multiple dot-separated fragments in the first version. Do not claim that the current CLI already checks this.
- Generate a random `job_id`, persist the input, and dispatch the job. Return a normal submission response **only after broker confirmation of dispatch**. Do not wait for computation to finish; if the worker starts before the response, return the actual current state. Follow section 8.1 for failed or uncertain dispatch.

`request_id` is an optional unique retry key. Retrying the same key returns the existing `job_id` without creating another computation. Generate keys with sufficient randomness, such as UUID4, and treat them as private. State that this anonymous service has no per-user namespace for retry keys either.

- Compare inputs after trimming SMILES at both ends and deduplicating/sorting nuclei. Do not assume different SMILES representations are chemically identical.
- The same key and input return the same job whether it is processing, completed, failed, cancelled, or expired. Recomputing a failed job requires an explicit submission with a new key.
- If confirmation of the existing job's dispatch remains uncertain, return the same `job_id` with `DISPATCH_UNCERTAIN` rather than disguising it as normal submission success. If execution or completion is confirmed, return that actual state.
- The same key with different input produces `REQUEST_ID_CONFLICT`. Serialize key reservation so concurrent submissions with the same key also resolve to one job.
- Persist the key-to-job mapping together with the job record for 7 days after the terminal state is confirmed. Explain that a key may be treated as a new request after this guarantee expires.
- A submission without a key creates a new job each time. Resubmitting without a key after a network error can duplicate computation. The client must retain the key before submitting so it can reuse it if the response is lost.

### 5.2 get_job_status

Query status, UTC timestamps, execution stage, and reportable errors by `job_id`. Use the parent plan's states: `queued`, `running`, `cancelling`, `completed`, `failed`, `cancelled`, and `expired`.

The existing CLI does not provide structured percentages or ETAs. Initially return status-focused responses; do not convert stdout progress messages into invented percentages.

### 5.3 get_job_result

Return a short summary of a completed job, atom/conformer counts by nucleus, a small preview of weighted atom shifts, the unit `ppm`, file names/links, and `expires_at`.

Record the CASCADE revision, model filenames, and computation-environment versions. Describe results as chemical-shift predictions for a candidate molecule, not automatic structure identification or complete NMR spectrum simulation. Do not apply the paper's average error as a guarantee for an individual molecule. Preserve the original LICENSE and the publication citation from README in deployment documentation.

Do not dump entire CSV files into chat. Label previews as partial and include total row counts and links to complete CSV files. Returning a URL alone does not mean the language model has read the file.

Always provide a PNG result link. Implement and test returning MCP image content as well when supported by the client. Do not guarantee automatic inline rendering across clients. PNG/SVG downloads must remain available even when a client cannot display the image.

Repeated queries for the same job return stored results without rerunning computation. Clearly report expired or incomplete jobs.

### 5.4 cancel_job

Record cancellation intent for `job_id`. Recheck cancellation immediately before executing queued jobs; for running jobs, the worker's monitoring loop terminates the computation process group. Do not report `cancelled` before confirming termination.

Do not add a separate `get_conformer` tool or a tool listing all jobs in the first version. Provide per-conformer values through CSV files.

### 5.5 Shared Response and Error Contracts

The following JSON examples are application responses carried in MCP `structuredContent`. Also include the same core information in short text content. Job IDs and URLs are placeholders; actual IDs are long cryptographically random values. All timestamps use UTC ISO 8601.

For every tool, validate the format and length of `job_id` **before** using it in a path. Initially generate IDs with `secrets.token_urlsafe(32)` and allow only a full match of 43 URL-safe characters. Do not interpret slashes, dot paths, URL-encoded strings, or NUL as paths. Format errors produce `INVALID_INPUT`; a well-formed but nonexistent ID produces `JOB_NOT_FOUND`. Also restrict `request_id` format/length and use a digest rather than the raw value as the private index filename. Apply the same validation and existing-record check to job IDs from the queue; do not create a job directory based on a message alone.

**Successful submission:** `reused` indicates whether the retry key returned an existing job. The `status` of a reused response is the existing job's current state.

```json
{"ok": true, "job_id": "EXAMPLE_JOB_ID", "status": "queued", "reused": false}
```

**Running status query:** `stage` is optional; omit percentages and ETA when no verified values exist. `started_at`, `finished_at`, and `expires_at` are `null` until their respective events occur.

```json
{"ok": true, "job_id": "EXAMPLE_JOB_ID", "status": "running", "stage": "prediction", "created_at": "2026-10-21T16:00:00Z", "started_at": "2026-10-21T16:00:01Z", "finished_at": null, "expires_at": null}
```

**Completed result:** the following illustrates a `13C`-only response. Preview rows and file entries are abbreviated; actual responses include all generated public files and the ZIP. Atom values illustrate data types.

```json
{
  "ok": true,
  "job_id": "EXAMPLE_JOB_ID",
  "status": "completed",
  "summary": "13C 예측 완료",
  "finished_at": "2026-10-21T16:01:00Z",
  "expires_at": "2026-10-22T16:01:00Z",
  "results": {
    "13C": {
      "atom_count": 8,
      "conformer_count": 1,
      "unit": "ppm",
      "atom_shifts_preview": [{"atom_index": 1, "shift_ppm": 21.43}],
      "preview_truncated": true
    }
  },
  "files": [{"name": "13C_prediction_2d.png", "mime_type": "image/png", "url": "https://cascade.example.com/results/EXAMPLE_JOB_ID/13C_prediction_2d.png"}]
}
```

**Incomplete result query:** this is a normal status response, not a tool error. For `queued`, `running`, or `cancelling`, omit `results` and return an empty `files` list. Queries for failed/cancelled jobs also succeed with `ok: true`; include a sanitized job error for failures.

```json
{"ok": true, "job_id": "EXAMPLE_JOB_ID", "status": "running", "files": []}
```

```json
{"ok": true, "job_id": "EXAMPLE_JOB_ID", "status": "failed", "error": {"code": "INVALID_MOLECULE", "message": "분자 입력이 허용 범위를 벗어났습니다."}, "files": []}
```

**Cancellation response:** a running job first enters `cancelling`. A queued job can be confirmed as `cancelled` after atomically preventing execution. `cancel_accepted` indicates that this request newly recorded cancellation intent; `cancel_requested` is the persisted intent. Jobs already terminal, already committing completion, or already requested for cancellation return their current state and `cancel_accepted: false`. A cancelled job keeps `cancel_requested: true`; repeated requests do not introduce new side effects.

If publication was interrupted and requires recovery, return the job ID with `SERVICE_UNAVAILABLE` and `retryable: true` instead of presenting an unconfirmed state as completed. After recovery, another query/cancellation returns the confirmed state.

```json
{"ok": true, "job_id": "EXAMPLE_JOB_ID", "status": "cancelling", "cancel_accepted": true, "cancel_requested": true}
```

**Expiry and missing IDs:** while the record remains, return a normal `expired` response and `files: []` without returning old URLs. If the record retention period has also elapsed, or the ID does not exist, return `JOB_NOT_FOUND`. Querying an expired job must not start another computation.

```json
{"ok": true, "job_id": "EXAMPLE_JOB_ID", "status": "expired", "expires_at": "2026-10-22T16:01:00Z", "files": []}
```

```json
{"ok": false, "error": {"code": "JOB_NOT_FOUND", "message": "작업 기록을 찾을 수 없습니다.", "retryable": false}}
```

When a requested operation on a job cannot be performed, return `ok: false` and set MCP tool result `isError` to `true`. Invalid JSON-RPC/MCP requests follow the SDK's protocol-error handling. Application error codes are `INVALID_INPUT`, `REQUEST_ID_CONFLICT`, `JOB_NOT_FOUND`, `QUEUE_UNAVAILABLE`, `DISPATCH_UNCERTAIN`, `SERVICE_UNAVAILABLE`, and `ARTIFACT_UNAVAILABLE`. For queue, dispatch, and service failures, use `retryable` to indicate whether retrying with the same `request_id` is appropriate. Include `job_id` in error responses if a job has already been created.

If a completed record exists but required public files are lost/corrupted before retention expires, return the application error `ARTIFACT_UNAVAILABLE`; do not return invalid links or automatically recompute. Do not overwrite the completion history with a new failed job state. Expired files instead produce a normal `expired` response. Reject storage failures or admission-limit violations promptly with `SERVICE_UNAVAILABLE`, rather than reporting successful admission. Explicitly state retryability with `retryable`.

Job execution error codes are `INVALID_MOLECULE`, `PREDICTION_FAILED`, `ARTIFACT_VALIDATION_FAILED`, `TIMEOUT`, `WORKER_LOST`, and `DISPATCH_FAILED`. Keep tracebacks and internal paths in private logs; return only sanitized causes to clients.

## 6. Job Execution and Reusing the Existing CLI

A worker job follows this sequence:

1. Acquire the execution lock and, under a short state lock, check the existing job, deployment, and cancellation intent. Atomically transition only an executable `queued` job to `running`/`stage=starting`. Duplicate messages must not gain execution rights concurrently, and cancelled jobs must not restart.
2. Prepare the job-specific directory and log files.
3. Run the existing `predict_2d.py` with legacy Python inside the same container. Pass arguments as a list, not a constructed shell string.
4. Set `cwd` and `PYTHONPATH` to `/src/cascade-Jupyternotebook-SMILES/models`. Use the image-copied source/models from section 4; unfinished previous-worker jobs are failed at startup.
5. Write stdout/stderr to files while monitoring process exit, cancellation, and time limits. Avoid pipe deadlocks caused by unread logs.
6. Validate the exit code and files for every requested nucleus. Exit code zero alone does not establish success.
7. Prepare the summary and public artifacts, then publish atomically. Resolve cancellation races using section 8.1's completion-commit rules; record `completed` and completion/expiry timestamps.

Changing only the existing CLI's `--output` to a job-specific path allows reuse of its per-nucleus file structure. Do not renumber atoms or independently reorder the molecular structure. Preserve atom correspondence across SDF, CSV, and PNG.

The current CLI performs separate conformer searches for `1H` and `13C`. Do not assume matching conformer numbers across the two results represent the same geometry. If either requested nucleus fails, the first version marks the entire job `failed` and publishes no partial results.

Recheck cancellation immediately before launch, and coordinate `Popen` with recording process ownership under the state lock. Failures between process creation and recording are handled by section 8.2's recovery rules. Do not hold the state lock during long model loading or computation.

## 7. Result Storage and Publication Contract

Mount cascade's host storage at `/data/jobs` in MCP, worker, and maintenance containers. Use the following job-directory layout:

```text
/data/jobs/<job_id>/
  input/request.json         Private input
  logs/stdout.log            Private execution logs
  logs/stderr.log
  job.json                   Private job record
  work/1H/                   Temporary CLI results
  work/13C/
  public/                    Files published after validation
```

Keep existing filenames and per-nucleus folders for internal CLI output. Prefix public filenames with the nucleus to match the parent plan's single `<filename>` URL:

- `1H_weighted_shift.csv`, `1H_conformers_shift.csv`, `1H_conformers.sdf`, `1H_prediction_2d.png`, and `1H_prediction_2d.svg`.
- Apply the same rule to `13C_…`. Generate only requested nuclei.
- `results.zip`: include only requested nucleus folders and artifacts. Exclude raw inputs, logs, job records, and temporary files. ZIP packaging is implemented in the monitored pipeline.

Current CSV files store ppm values in `Shift` or `predicted`; SDF files contain 3D coordinates and conformer information. Build summaries by parsing the files. Validate atom-level data, conformer counts, finite numerical values, SDF parsing, and PNG/SVG validity. Do not publish if atom indices do not match.

**Validate conformer correspondence by ID, not file order.** SDF `_Name` uses `mol_id_cf_id`; for example, `0_196` corresponds to CSV `mol_id=0`, `cf_id=196`. Values of `cf_id` are not consecutive 0–N indices, and SDF record order can differ from CSV sort order. For each nucleus, verify that SDF and CSV conformer ID sets match and that each conformer has no missing/duplicate expected atom IDs. Do not link conformer IDs across different nuclei.

A further investigation of the flexible molecule `CCCCO` with `13C` generated 7 conformers. Sorted CSV IDs were `[10, 26, 54, 109, 138, 175, 196]`, while SDF order was `[10, 196, 26, 138, 175, 109, 54]`. Do not hard-code these values or counts as expected results; prepare **a test fixture retaining multiple conformers** to validate ID-based mapping. The current CLI rounds `b_weight`, `relative_E`, and `predicted` before storing them, so weighted averages recomputed from these values need not match the original internal computation bit for bit.

The shared Nginx mounts `/project_root/mcp-results` read-only at `/data/results`. cascade's `/results/<job_id>/<filename>` requests must map only to `/data/results/cascade/<job_id>/public/<filename>`. Implement an allowed-filename list and block path traversal/symlinks. Do not expose `input`, `logs`, `work`, or `job.json`.

Example public URLs:

- Local: `https://YOUR_NGROK_DOMAIN/cascade/results/<job_id>/13C_prediction_2d.png`.
- Production: `https://cascade.example.com/results/<job_id>/13C_prediction_2d.png`.

Accordingly, `PUBLIC_BASE_URL` is `https://YOUR_NGROK_DOMAIN/cascade` locally and `https://cascade.example.com` in production. Do not return `localhost`, container names, or host filesystem paths as download addresses.

The first version has no login or OAuth. Anyone holding a link can download; anyone holding a job ID can query or cancel it. Use long random IDs as in the parent plan, and do not expose a complete job listing.

## 8. Cancellation, Failures, and Retention

- Start with worker concurrency 1. Also tune CASCADE/numerical-library internal threads to host CPU/memory limits.
- Acquire an execution lock before starting a job; do not execute redelivered copies of the same job concurrently. Do not use the Celery result backend as the sole job record.
- Use persistent `job.json` as the authoritative state and treat Redis state as a recoverable cache. Serialize state transitions using section 8.1's locking/revision rules.
- Launch computation in a new process group. On cancellation/timeout, send a termination signal, allow a grace period, force termination if necessary, and confirm exit. Do not cancel one job merely by calling Celery revoke or stopping the entire worker container.
- Test completion/cancellation races, computation processes left after abnormal worker exit, and Redis redelivery. Select acknowledgement, visibility-timeout, and retry policies together with maximum runtime; do not automatically recompute unconditionally.
- Use `failed` with `termination_reason=timeout` for maximum-runtime violations, and `cancelled` for user cancellation after termination is confirmed.
- Retain successful results for 24 hours after completion and failed/cancelled logs for 24 hours after state confirmation. Maintenance must not delete files during execution or publication preparation.
- Use a cleanup interval such as 5 minutes, as in the parent plan; accept that much delay in expiry enforcement for static downloads. Provisionally retain job records for 7 days to report `expired`.

Set actual input-length/atom-count limits, maximum runtime, cancellation grace period, memory/CPU limits, and queue limits during implementation based on the host and measured examples. Do not present unvalidated numbers as official CASCADE limits.

### 8.1 Recovery Rules for Job Records, Dispatch, and Completion Commit

The initial implementation uses the existing shared job storage and Redis without adding a database, but does not operate them as independent sources of state.

**Authoritative records and locks**

- In addition to status, store an increasing `revision`, `dispatch_state`, a fixed `celery_task_id`, `deployment_id`, execution-process identification, and `publication_state` in `job.json`. If the Redis cache conflicts, prefer `job.json`.
- MCP, worker, and maintenance acquire the shared file lock for a job before reading and modifying its latest record. Update records through temporary-file writes, flush/fsync, and atomic replacement on the same filesystem, then update Redis. Do not rely solely on Redis locks, so a Redis disconnect/restart cannot by itself remove execution locking. Test cross-container file locking and atomic rename on the actual shared volume.
- Distinguish job-assignment locking from the execution lock held for the entire computation. Do not hold the state lock throughout computation and block cancellation requests, queries, or maintenance.
- Lock separate stable files under `/data/jobs/.locks/`, not `job.json`, which is atomically replaced. Do not unlink active lock files or delete them with job directories. Use a consistent acquisition order: retry key → execution → state; do not acquire an earlier lock while holding the state lock. Cleanup checks execution locks nonblockingly and skips jobs in use. Prune lock files only during separate maintenance with all relevant processes stopped.
- Store `request_id` mappings in a private index on the same persistent storage, and serialize reservation of the same key with locking. Persist inputs, job records, and key mappings recoverably before sending to the broker. After interruption during reservation, clean up or recover the same job on restart rather than unconditionally creating another.

**Dispatch confirmation and uncertain delivery**

1. When creating the persistent record, store internal `dispatch_state=pending` and one fixed `celery_task_id`. This does not add a state to the public status list.
2. Send to Celery using this task ID and `job_id`. After successful dispatch confirmation, set the record to `confirmed` and return a normal submission response. Apply short, finite limits to dispatch confirmation/retries so MCP calls do not remain blocked. If the worker has changed state meanwhile, reread the latest record and preserve that state.
3. If a disconnect or similar failure makes broker receipt uncertain, record `dispatch_state=uncertain` and return `DISPATCH_UNCERTAIN`, `retryable: true`, and the same `job_id`. The client retries with the same `request_id` or queries that job.
4. Startup/periodic recovery finds `pending`/`uncertain` jobs and resends with the same task ID or checks whether execution has already started. Recover or fail stale `queued` jobs lost in broker failures within a defined waiting deadline. Do not leave them `queued` indefinitely.
5. Resending can create duplicate messages. **Do not assume the same Celery task ID alone prevents duplicate execution.** Workers check the execution lock and `job.json`; they do not recompute running or terminal jobs. Delayed messages for jobs already confirmed failed must not start computation either.
6. If dispatch is definitively impossible or the recovery deadline expires, record `failed` with `DISPATCH_FAILED`. Do not return normal submission success before broker confirmation. `QUEUE_UNAVAILABLE` can be retryable before job creation; if a job has already been confirmed failed, return its `job_id` and `retryable: false`. A new computation requires a new retry key.

These rules address response loss and duplicate messages; they do not claim exactly-once execution in Celery. Actual recovery tests must pass for broker failures and each process-interruption point.

**Execution failures and publication commit**

- Persist execution start and record the computation process's job marker, PID/process group, and start identifier. On restart, do not kill another process based solely on a PID number. Do not rerun the same job before confirming that its previous computation has ended.
- Initially, interrupted `running` jobs are not automatically recomputed. Clean up remaining computation processes, then confirm `failed` with `WORKER_LOST`. If publication commit has already started, prefer the recovery rules below.
- Validate all files in a private staging directory. Under the final state lock, check cancellation; if cancellation was recorded first, do not publish. Otherwise, first persist the artifact manifest and `publication_state=committing`. This is the completion commit point.
- Reject cancellation after this point. Atomically rename the validated directory to `public/` on the same filesystem, then record `completed` and completion/expiry timestamps. During this short interval, static files may become visible before query status reports completion, but partial files must not be published.
- If interrupted after rename but before recording state, inspect the `committing` record and artifacts and finish committing completion. If only the staging directory remains, finish the same publication procedure. For unrecoverable file errors, remove public files and confirm `ARTIFACT_VALIDATION_FAILED`. Recover through startup/maintenance procedures; until completion, return `SERVICE_UNAVAILABLE` for queries/cancellation. Do not wait for lengthy recovery inside an MCP call.
- Set terminal/expiry timestamps once from the confirmed record; queries and restarts do not renew the 24-hour period. Clean up duplicate `work/` only after confirming preservation of validated original results, and explicitly include partial failed/cancelled results in cleanup policy.

If records cannot be reconstructed or process ownership cannot be established, stop accepting new computations and report an error requiring operator investigation. Do not change state to `completed`/`cancelled` or rerun automatically without evidence.

### 8.2 Startup, Process Ownership, and Interrupted Cleanup Boundaries

**Execution and termination**

- Readiness checks verify the legacy interpreter, CLI/model paths, storage write permissions, and available space. Reject admission if readiness fails. Check model loading during deployment validation; an open port alone does not establish computation readiness.
- Serialize the `queued → running` claim and cancellation check. If cancellation was confirmed first, do not create a process. If cancellation arrives immediately after creation, terminate/reap the recorded process group before confirming `cancelled`. Treat molecules without atoms for a requested nucleus as `INVALID_MOLECULE` during pre-computation validation.
- Missing executables, model-loading failures, and abnormal exits produce `PREDICTION_FAILED`. Preserve exit codes/signals in private logs, but do not infer causes such as OOM from a signal alone. Distinguish timeout/user cancellation using confirmed termination causes.
- Job time limits cover model loading, conformer searches, both nuclei, rendering, artifact validation, and ZIP preparation. Monitor with a monotonic clock in the control environment. After the publication commit point, use a finite publication-recovery deadline instead of cancellation. Validate termination paths across computation and postprocessing so blocking Python work cannot stall monitoring.
- Exceptions, log-write failures, and timeouts must all pass through termination cleanup; confirm remaining child processes have exited before releasing the execution lock. If record persistence itself fails, do not report success and restrict admission. After storage recovery, reconcile existing records and artifacts before confirmation; never publish partial files as successful results.

**Cross-container ownership and recovery responsibilities**

- Process identification includes the worker instance and a start identifier. Check PIDs only inside the owning worker's PID namespace. Do not inspect/terminate the same numerical PID in the maintenance container or infer worker loss merely because that PID is absent there.
- Monitoring/termination inside the worker owns cleanup of computation children. Cover cases where only the Celery execution process dies while the container remains alive. If interruption occurs immediately after `Popen` but before PID recording, the job marker must allow ownership identification. Distinguish termination of the entire worker container from termination of an individual execution process. Do not make Docker socket access or shared PID namespaces between containers a default requirement.
- Maintenance owns record, dispatch, publication, and expiry recovery. Check both execution locks and owner-worker liveness; restarting only MCP/Redis must not mark a live computation `WORKER_LOST`. Even if an execution lock is released, cleanup is not complete until remaining computation processes are confirmed terminated. Leave jobs with uncertain ownership for operator investigation.

**Expiry and resuming cleanup**

- Queries determine expiry from stored `expires_at` and return `expired` with an empty file list. Physical deletion of static files may lag by the defined cleanup interval. Missing required files before expiry instead produce `ARTIFACT_UNAVAILABLE`.
- Under locking, confirm a job is neither executing nor publishing, persist cleanup intent, then delete public files, temporary results, and diagnostic files whose retention period has elapsed. Cleanup must be repeatable after interruption; publication recovery must not republish already expired artifacts or artifacts being deleted.
- After the 7-day record-retention period following terminal confirmation, remove the corresponding `request_id` mapping and cache consistently. Delayed Celery messages must not recreate job directories or resume computation when the authoritative record is absent. Do not delete separate stable lock files during this online cleanup.

## 9. Files to Add and Treatment of Existing Files

The following modules are now implemented under CASCADE/mcp. The standalone Dockerfile and Compose file remain separate; see mcp/README.md for validation boundaries.

- `mcp/src/cascade_mcp/server.py`: the four MCP tools and input/response schemas.
- `mcp/src/cascade_mcp/tasks.py`: bounded Celery dispatch and task registration; `mcp/src/cascade_mcp/worker.py` supervises execution, cancellation and time limits.
- `mcp/src/cascade_mcp/jobs.py`: authoritative records, state transitions, locking, retry-key mappings, dispatch recovery, and expiry timestamps.
- `mcp/src/cascade_mcp/artifacts.py`: file validation, summary extraction, public filename conversion, ZIP/URL generation.
- `mcp/src/cascade_mcp/maintenance.py`: expired-result and diagnostic-log cleanup.
- `mcp/Dockerfile`: multi-stage legacy/control/worker builds with automatic metadata, selected by Compose targets.
- `mcp/compose.mcp.yml`: `cascade` services, dedicated Redis, external proxy network, and shared storage mounts.
- Modern-environment `pyproject.toml`/`uv.lock`, environment-variable examples, job-lifecycle tests, and client connection instructions.

Reuse the existing `predict_2d.py`, legacy dependency list, and environment checks. Add remote-execution input/result validation in `mcp/`; do not modify the original models or `nfp`. In `mcp/README.md`, distinguish standalone and remote execution and document commands for each.

The root and `mcp/` AGENTS.md files now distinguish standalone and remote invariants. Local tests establish only the explicitly exercised boundaries; named clients and production deployment remain separate acceptance work.

## 10. Implementation Order and Acceptance Criteria

1. **Preserve the standalone baseline:** pass both example-nucleus predictions, atom-mapping checks, and file-format checks with the existing Compose configuration. This baseline is already established.
2. **Worker with two Python environments:** run the legacy CLI from modern Python inside the same container and verify package isolation. If this fails, resolve image construction before implementing MCP.
3. **Job lifecycle:** connect dedicated Redis, worker, and persistent paths; validate dispatch, execution, failure, cancellation, restart, and expiry. Inject failures after input persistence, after dispatch but before responding, and after public rename but before state persistence to check section 8.1's recovery rules. Distinguish Redis cache loss/conflicts from actual execution-process loss.
4. **Four MCP tools:** return normal submission responses after broker confirmation; confirm repeated status/result queries do not create computations. Sequential/concurrent retries with the same key must yield the same job ID, while different input must conflict. Test response loss, incomplete-result queries, missing IDs, 24-hour expiry, and 7-day record deletion against section 5.5. Do not display invented progress.
5. **Public file integration:** coordinate public-path mapping with the `mcp-infra` project. Every CSV/SDF/PNG/SVG/ZIP link must download successfully; internal files must remain inaccessible.
6. **Local external connection:** as in parent appendix A, use Nginx HTTP and one ngrok instance, registering `https://YOUR_NGROK_DOMAIN/cascade/mcp`. Do not run Certbot for the basic local test.
7. **Validate all four clients:** test the four tools and downloads in chatgpt.com, ChatGPT Desktop, claude.ai, and Claude Desktop using actual accounts/app versions. Success in one environment does not establish success in another.
8. **Production and multiple projects:** validate resources/architecture on the actual host and switch to `https://cascade.example.com/mcp`. After adding bbb, confirm queues, results, cancellation, and cleanup do not cross project boundaries.

Prefer x86_64 Linux for the host. The current image is `amd64`; Apple Silicon Mac mini or AWS EC2 requires separate validation of emulation and throughput. Do not add GPUs or model retraining as initial requirements.

The basic completion criteria cover the example SMILES with `1H` alone and `13C` alone; invalid input; failure of one requested nucleus; two consecutive submissions; queued/running cancellation; timeout; restart; expiry; and private/public download boundaries. At deployment, stop admission and wait for queued/running jobs to finish or explicitly cancel them before replacing workers.

Additional completion criteria:

- Use a flexible-molecule fixture with multiple conformers to map CSV `cf_id` to SDF `_Name` by ID; verify the same mapping after changing SDF order. Account for rounding in stored values when comparing averages.
- Duplicate requests/response loss with the same retry key must not create extra computations. Retrying failed, cancelled, or expired jobs must not silently start another computation.
- Deliberately race cancellation against publication commit. Before the commit point, cancellation must produce a private `cancelled` job; after it, cancellation must be rejected and completion committed. Check file removal and error state when recovery from publication interruption fails.
- Verify direct Compose rebuild/restart needs no manual image settings or drain; old unfinished jobs fail clearly and completed results survive. Keep model hashes and build provenance for diagnostics.
- In each client, test application-error `ok`/MCP `isError`, normal tool responses when querying failed jobs, and the distinction between `cancel_accepted` and `cancel_requested`.
- Inject cancellation/interruption between job claim, process creation, and PID recording. Separately verify child cleanup when only the Celery execution process dies and preservation of running computations when only MCP/Redis restarts.
- Inject missing executables, model-loading failures, storage exhaustion, and rendering/ZIP timeouts; verify no partial results become public and no processes remain. Confirm separate locks prevent concurrent state changes while atomic replacement is repeated.
- Reject path traversal/invalid formats on every ID input path, and distinguish loss of required files during retention. Interrupted/resumed expiry cleanup, record/retry-key deletion, and delayed messages after deletion must not recreate jobs or extend retention.

## 11. Items to Confirm at Handoff

- Actual `/project_root`, CASCADE checkout location, domains, shared result paths, and file permissions.
- Modern Python/FastMCP/Celery versions and build results for the worker's two environments.
- Atom-count, input-length, runtime, queue, CPU, and memory limits, plus cancellation grace period.
- Shared-volume locking/atomic-rename validation for section 8.1, and actual acknowledgement, visibility-timeout, and dispatch-recovery-deadline settings. Initially retain records/retry keys for 7 days after terminal confirmation.
- Image-display/file-download behavior in each client. Record support after actual connection tests.
- Whether and when an external 3D viewer is needed. Keep it separate from initial MCP-tool acceptance criteria.

The first implementation target under this document is **the cascade layer wrapping CASCADE execution and results**. Share responsibilities with the common infrastructure plan, but do not duplicate other analysis servers or shared TLS configuration in this repository.

## References

- Parent plan: sections 2–4 and appendix A of `multi_mcp_server_prototype_plan.md`. Adjust the relative link at the top if sibling repositories use a different layout.
- Current execution/compatibility information: [README.md](./CASCADE/mcp/README.md), [AGENTS.md](./CASCADE/mcp/AGENTS.md), [Root AGENTS.md](./CASCADE/AGENTS.md).
- [FastMCP: Running Your Server](https://gofastmcp.com/deployment/running-server): HTTP transport and server execution.
- [Celery: Workers Guide](https://docs.celeryq.dev/en/stable/userguide/workers.html): worker termination/cancellation constraints.
- [Celery: Calling Tasks](https://docs.celeryq.dev/en/stable/userguide/calling.html): message-dispatch errors/retries and explicit task IDs.
- [Celery: Using Redis](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html): visibility timeout and message-redelivery constraints.
- [Python 3.12: subprocess](https://docs.python.org/3.12/library/subprocess.html): separate-interpreter execution and process management.
- [Linux: flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html): file-lock lifetimes and the rationale for separate stable lock files.
