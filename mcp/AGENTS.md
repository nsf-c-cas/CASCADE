# CASCADE standalone and MCP integration

- This directory belongs to the parent CASCADE repository. Preserve upstream science/models. Standalone mounts `../` read-only and writes `/output`; remote images copy immutable source/models and write `/data/jobs`.
- One multi-stage `Dockerfile` supplies legacy/control/worker targets. Standalone `compose.yml` selects legacy; remote `compose.mcp.yml` builds control and worker automatically.
- Inference imports the bundled `nfp`; do not install a different `nfp` from PyPI.
- Run from `/src/cascade-Jupyternotebook-SMILES/models`: upstream code opens model and preprocessing files using relative paths.
- Python 3.5 cannot use uv. Keep the legacy runtime separate from modern MCP dependencies.
- `IMPORT OK` proves imports only. Model loading, inference, and output files require separate runtime validation.
- The additional dependency pins are candidates until a successful image build and inference run; they are not a full lockfile.
- The Conda environment includes `mkl-random`, whose package metadata requires `cython`. Keep the Python 3.5-compatible Cython pin so `pip check` can validate the whole environment.
- The legacy RDKit binary needs X rendering shared libraries even for CLI imports; install them in the image rather than modifying upstream code.
- External check/adapter scripts need the upstream models directory on `PYTHONPATH` to import `cascade` and the bundled `nfp`; the working directory alone is insufficient when executing a script by absolute path.
- Use Keras 2.2.4: both upstream `webapp_env.yml` files specify it and both SMILES model files record `keras_version: 2.2.4`. The top-level environment and README instead specify 2.2.5, which fails the bundled `nfp` import. Keep upstream unchanged; document the recommended upstream version alignment in this directory's README.
- With Keras 2.2.4, image build, `pip check`, dependency imports, and prediction-function imports (`IMPORT OK`) pass. `predict_2d.py` also loads both models and predicts the example SMILES in the README (10 H shifts, 8 C shifts); this is not general accuracy validation.
- The CLI preserves one-based upstream atom IDs in its PNG/SVG labels and checks them against the selected nucleus. Keep explicit hydrogens visible for both proton and carbon drawings, matching the website. CSV/SDF outputs retain the upstream ordering; avoid drawing from an independently reordered SMILES molecule.
- `predict_2d.py` is copied into the image: rebuild after editing it. Moving the directory or editing the README alone does not require a rebuild.
- When verifying SDF output with this legacy RDKit, iterate `SDMolSupplier` with a comprehension rather than `list(supplier)`: the latter returned an empty list for the valid example file, while indexed access and comprehension successfully read it.
- Keep CLI warning filters limited to the known TensorFlow dtype and sklearn import notices by category, message, and module. Do not hide all warnings or stderr; keep errors and progress visible. `TF_CPP_MIN_LOG_LEVEL=1` hides C++ info only and may be overridden by the caller.

## MCP contracts

- Application modules live in `src/cascade_mcp/`; run entry points as `python -m cascade_mcp.<module>`. Containers set `PYTHONPATH=/app/src`; pytest sets `pythonpath = ["src"]`. Standalone utilities and build helpers live in `scripts/`. Keep `__init__.py` free of modern imports so the Python 3.5 adapter can also be imported independently.
- Keep `README.md` focused on run commands; use `DEPLOY_EC2.md` for production deployment and `README_details.md` for runtime/scientific explanations. Application production commands select `.env.production`; shared ingress also selects its bootstrap/prod override.
- Modern dependencies are pinned in `pyproject.toml`/`uv.lock`; run `uv run pytest -q`. Opt-in real stack: `RUN_CASCADE_DOCKER_TESTS=1 uv run pytest -q`.
- Use direct Compose `up -d --build` with explicit `--env-file`/`-f`. Stable tags, automatic metadata; no `.env.images`, `CASCADE_LEGACY_IMAGE`, manual manifest/pin command or build ordering. `scripts/deployment.py` is an internal Docker build helper.
- Accept one nucleus per remote job, canonicalizing Hydrogen/Carbon aliases. Validate chemistry in the unchanged legacy runtime through `legacy_adapter.py`.
- Reuse the existing CLI through absolute subprocess arguments, never a shell. Validate SDF/CSV conformers by `(mol_id, cf_id)`, never record order.
- Job IDs fully match `[A-Za-z0-9_-]{43}` before any path access. Retry keys use private hashed index filenames; persisted reservation recovery reuses the same ID.
- Stable locks live under `.locks/`: 256 stripes each for retry/execution/state/dispatch, plus admission/worker (at most 1,026 new files). Keep lock types in separate pools to preserve ordering; collisions serialize unrelated keys. Acquire retry → execution → state. Never unlink online locks or lock atomically replaced job.json files. Stop all application processes before upgrading from per-key locks; old files are retained, not auto-deleted.
- Pass the execution-lock FD through the pipeline and legacy child. Supervise Celery solo separately so its death cannot leave unowned computation children.
- The durable publication_state=committing plus manifest is the cancellation cutoff; atomically rename staging to public, then confirm completion. Recovery never publishes partial/expired cleanup data.
- Research deployments may stop/rebuild anytime. Worker startup fails old unfinished jobs with `WORKER_LOST`, recovers validated publication commits, preserves completed results, and clears optional old pause flags. No mandatory drain or exact image-ID guard. Keep matching-build heartbeat, chemistry/file validation and safe cleanup. Preserve records/retry keys seven days and artifacts/logs 24 hours by default.
- MCP application errors return structured ok=false and isError=true; querying failed/cancelled/expired jobs is a normal tool result.
- Configure explicit public Host/Origin allowlists; only MCP joins mcp-proxy. No published MCP/Redis ports and no Docker socket.
