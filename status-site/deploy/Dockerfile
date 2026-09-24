FROM python:3.12-slim
ARG CODEX_VERSION=0.156.1
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm git ca-certificates && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex@${CODEX_VERSION}
WORKDIR /opt/modeltrace
COPY fingerprint.py ./
COPY data/unified_bank.json ./data/unified_bank.json
COPY status-site ./status-site
RUN pip install --no-cache-dir ./status-site
ENV MODELTRACE_STATUS_CONFIG=/config/config.toml
CMD ["gunicorn", "--bind", "0.0.0.0:7861", "--workers", "1", "--threads", "4", "modeltrace_status.web:create_app()"]
