FROM mambaorg/micromamba:2.9.0 AS legacy

# Keep the upstream CASCADE interpreter and RDKit versions.
RUN micromamba install -y -n base --override-channels \
    -c rdkit -c defaults python=3.5 rdkit=2018.03.03 pip \
    && micromamba clean --all --yes

ARG MAMBA_DOCKERFILE_ACTIVATE=1
RUN python -m pip install --no-cache-dir \
    pip==20.3.4 setuptools==50.3.2 wheel==0.36.2

COPY --chown=$MAMBA_USER:$MAMBA_USER mcp/requirements-legacy.txt /tmp/requirements-legacy.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements-legacy.txt \
    && python -m pip check

# Shared libraries required by the upstream RDKit binary, even without a GUI.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*
USER $MAMBA_USER
RUN python -c "import tensorflow, keras, numpy, pandas, scipy, sklearn, h5py; from rdkit import Chem; print('DEPENDENCIES OK')"

COPY mcp/scripts/check_environment.py /opt/cascade-test/check_environment.py
COPY mcp/scripts/predict_2d.py /opt/cascade-test/predict_2d.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PYTHONPATH=/src/cascade-Jupyternotebook-SMILES/models
WORKDIR /src/cascade-Jupyternotebook-SMILES/models
CMD ["python", "/opt/cascade-test/check_environment.py"]

FROM ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 AS uv
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS control-base
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/control UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY mcp/pyproject.toml mcp/uv.lock ./
RUN uv sync --frozen --no-dev

FROM control-base AS metadata
COPY mcp/src /build/mcp/src
COPY mcp/scripts /build/mcp/scripts
COPY mcp/pyproject.toml mcp/uv.lock mcp/requirements-legacy.txt mcp/Dockerfile /build/mcp/
COPY cascade-Jupyternotebook-SMILES/models /build/cascade-Jupyternotebook-SMILES/models
RUN /opt/control/bin/python /build/mcp/scripts/deployment.py

FROM control-base AS control
COPY mcp/src ./src
COPY --from=metadata /build/mcp/build/deployment.json ./deployment.json
ENV PATH=/opt/control/bin:/usr/local/bin:/usr/bin:/bin PYTHONPATH=/app/src
CMD ["/opt/control/bin/python", "-m", "cascade_mcp.server"]

FROM legacy AS worker
USER root
COPY --from=control /usr/local /usr/local
COPY --from=control /opt/control /opt/control
COPY --from=control /app /app
COPY cascade-Jupyternotebook-SMILES/models /src/cascade-Jupyternotebook-SMILES/models
COPY LICENSE README.md /usr/share/doc/cascade/
ENV PATH=/opt/control/bin:/usr/local/bin:/usr/bin:/bin PYTHONPATH=/app/src
WORKDIR /app
ENTRYPOINT []
CMD ["/opt/control/bin/python", "-m", "cascade_mcp.worker"]
