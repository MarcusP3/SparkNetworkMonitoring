FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SPARK_CONFIG=/config/spark.yaml

# iputils-ping is a fallback for environments where raw sockets aren't granted.
# Later increments add the Ookla speedtest CLI and openssh-client here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN mkdir -p /data /config

VOLUME ["/data", "/config"]
EXPOSE 9700

COPY docker/healthcheck.py /usr/local/bin/spark-healthcheck

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "/usr/local/bin/spark-healthcheck"]

CMD ["spark"]
