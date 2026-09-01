FROM python:3.12-slim

WORKDIR /srv/cortex

# onnxruntime (fastembed) needs libgomp
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install the package + dependencies first for layer caching
COPY pyproject.toml README.md ./
COPY cortex ./cortex
RUN pip install --no-cache-dir .

# Non-root
RUN useradd -m cortex && chown -R cortex:cortex /srv/cortex
USER cortex

EXPOSE 8738 8740
CMD ["uvicorn", "cortex.api.app:app", "--host", "0.0.0.0", "--port", "8738"]
