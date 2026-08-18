FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/
# The version the running app reads for /api/version + family_hub.__version__
# (family_hub.version resolves VERSION at the repo root, which is /app here).
# WITHOUT it the debug readout falls back to "0.0.0+unknown". (CHANGELOG.md is a
# GitHub doc, not read at runtime, so it isn't baked in.)
COPY VERSION ./
# Bake the EXAMPLE config as the in-image default so `docker build` works from a
# clean clone (the real config.json is gitignored). docker-compose bind-mounts
# the operator's real ./config.json over this at runtime; baking their private
# config instead would both couple the build to a gitignored file and leave real
# calendar IDs / LAN IPs sitting in an image layer.
COPY config.example.json config.json
ENV PYTHONPATH=/app/src CONFIG_PATH=/app/config.json DB_PATH=/data/hub.db TOKEN_PATH=/data/token.json
EXPOSE 8138
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8138/health')"
CMD ["python", "-m", "uvicorn", "family_hub.app:app", "--host", "0.0.0.0", "--port", "8138"]
