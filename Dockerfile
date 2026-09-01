FROM python:3.12-slim

WORKDIR /srv/cortex

# onnxruntime (fastembed) needs libgomp
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install the package + dependencies first for layer caching
COPY pyproject.toml README.md ./
COPY cortex ./cortex
RUN pip install --no-cache-dir .

# Non-root. /data/files is a named-volume mount point (docker-compose) for
# uploaded file blobs — created + owned here so the volume inherits the
# right ownership on first use (bind mounts don't get this for free).
RUN useradd -m cortex && chown -R cortex:cortex /srv/cortex \
    && mkdir -p /data/files && chown -R cortex:cortex /data/files
USER cortex

EXPOSE 8738 8740
CMD ["uvicorn", "cortex.api.app:app", "--host", "0.0.0.0", "--port", "8738"]
