# Axiom Graph — codemod-pattern service.
#
# Build (context = this directory):
#   docker build -t axiom-graph .
# Run:
#   docker run --rm -p 8000:8000 axiom-graph
# Then:
#   curl localhost:8000/health
#   curl -X POST localhost:8000/codemods -H 'content-type: application/json' \
#        -d '{"dependencies":[{"name":"flask","from_version":"2.0.3","to_version":"3.0.0"}]}'
FROM python:3.12-slim

# git is used by sources/git_releases.py (optional git-tag release discovery).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Then the package source. PYTHONPATH=/app makes `import axiom_graph` resolve.
COPY axiom_graph /app/axiom_graph
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Cache dir for sdists / griffe wheels (mount a volume here to persist).
ENV XDG_CACHE_HOME=/cache
RUN mkdir -p /cache

EXPOSE 8000

# A non-root user; the cache dir must be writable by it.
RUN useradd --create-home appuser && chown -R appuser /cache /app
USER appuser

CMD ["uvicorn", "axiom_graph.service.api:app", "--host", "0.0.0.0", "--port", "8000"]
