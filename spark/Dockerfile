# Base image: pin this to a digest, not a tag.
#
# `python:3.12-slim` is a moving target -- it is rebuilt regularly and points at
# different bytes over time, so two builds a month apart are two different
# images and neither is recorded. Get the current digest with:
#
#   docker pull python:3.12-slim
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
#
# then replace the line below with what it prints. Re-pin deliberately when you
# want a newer base -- which is the point: the upgrade becomes a commit you can
# see rather than a side effect of rebuilding.
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

# Dependencies before source, from hash-pinned locks.
#
# Two things this buys. The build becomes reproducible -- `pip install .`
# against `>=` constraints resolves fresh from PyPI every time, so you never
# build the same image twice and have no record of what shipped. And
# `--require-hashes` means a hijacked maintainer account is not enough to get
# code in here: an artifact has to match bytes recorded in this repository, or
# the build fails having installed nothing.
#
# The build toolchain is installed first, and hash-checked too. Without that,
# `pip install .` fetches hatchling unverified at build time, which is a hole
# straight through everything below it.
COPY requirements-build.lock requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-build.lock \
    && pip install --no-cache-dir --require-hashes -r requirements.lock

COPY pyproject.toml ./
COPY src ./src

# --no-deps because everything it needs is installed and verified above;
# --no-build-isolation because hatchling is too, rather than being fetched into
# a throwaway environment that nothing checks.
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

RUN mkdir -p /data /config

VOLUME ["/data", "/config"]
EXPOSE 9700

COPY docker/healthcheck.py /usr/local/bin/spark-healthcheck

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "/usr/local/bin/spark-healthcheck"]

CMD ["spark"]
