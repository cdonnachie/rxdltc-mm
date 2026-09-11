FROM python:3.12-slim

# Runs as an unprivileged user; the bot only needs network access to the KDF RPC
# (typically another container on the same compose network) and the price APIs.
RUN useradd --create-home --uid 10001 mmbot
WORKDIR /app

COPY pyproject.toml requirements.txt README.md ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --no-deps .

USER mmbot
ENV MM_BOT_CONFIG=/config/config.yaml \
    MM_BOT_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 9109
STOPSIGNAL SIGTERM

# --env-file is not read from /config by default: pass secrets via the container environment.
ENTRYPOINT ["rxdltc-mm"]
CMD ["--config", "/config/config.yaml", "--env-file", "/dev/null"]
