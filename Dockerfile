FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY watcher.py .

# Non-root user; PVC mount target must be writable by this uid.
RUN useradd --system --uid 1000 --home-dir /app --shell /usr/sbin/nologin watcher \
    && mkdir -p /data /config \
    && chown -R watcher:watcher /data /app

USER watcher

# Default paths align with the Kubernetes manifests in the README: ConfigMap
# mounted at /config, PVC mounted at /data. STATE_DB overrides the state_db
# value in config.yaml, so the same image works regardless of how the YAML
# happens to be filled in.
ENV PROTON_WATCHER_CONFIG=/config/config.yaml \
    PROTON_WATCHER_RULES=/config/rules.yaml \
    STATE_DB=/data/state.sqlite3

ENTRYPOINT ["python", "/app/watcher.py"]
