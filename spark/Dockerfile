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
# libcap2-bin is for the setcap below and nothing else.
# Later increments add the Ookla speedtest CLI and openssh-client here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping ca-certificates libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

# SPARK does not run as root. It holds a map of the network, every credential
# it has been given and -- with host networking -- a raw socket on the LAN; a
# bug in it, or in any of the 38 packages under it, should not also be uid 0
# on the VM's network namespace.
#
# A fixed uid rather than whatever adduser picks, so the ./data bind mount can
# be chowned to a number that stays the same across rebuilds:
#
#   sudo chown -R 9700:9700 data
#
# 9700 because it is the port, which is the one number about SPARK anyone has
# to remember anyway, and it is far above any uid a host is likely to use.
RUN groupadd --gid 9700 spark \
    && useradd --uid 9700 --gid 9700 --no-create-home --home-dir /app --shell /usr/sbin/nologin spark

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

# LICENSE.md beside pyproject.toml so hatchling ships it in the wheel's
# dist-info: the Required Notice has to travel with every copy.
COPY pyproject.toml LICENSE.md ./
COPY src ./src

# --no-deps because everything it needs is installed and verified above;
# --no-build-isolation because hatchling is too, rather than being fetched into
# a throwaway environment that nothing checks.
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

# Real ICMP needs CAP_NET_RAW, and a capability granted to the container with
# `cap_add` is only *effective* for root. For an unprivileged user it has to
# be attached to the executable, which is what a file capability does: the
# kernel adds it to the process at exec, provided it is also in the
# container's bounding set -- which `cap_add: [NET_RAW]` guarantees.
#
# The real binary, not the `python` symlink: file capabilities live in the
# inode's extended attributes and a symlink has none of its own.
#
# This is incompatible with `no-new-privileges`, which tells the kernel to
# ignore file capabilities at exec. Do not add it to the compose file: SPARK
# would start, fail to open a raw socket, and silently fall back to a ping
# that measures nothing.
RUN setcap cap_net_raw+ep "$(readlink -f "$(command -v python3)")"

RUN mkdir -p /data /config && chown spark:spark /data /config

VOLUME ["/data", "/config"]
EXPOSE 9700

COPY docker/healthcheck.py /usr/local/bin/spark-healthcheck

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "/usr/local/bin/spark-healthcheck"]

USER spark

CMD ["spark"]
