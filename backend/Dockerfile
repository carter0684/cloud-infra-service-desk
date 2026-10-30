# Cloud Infra Job Portal — backend production image
# Multi-stage build: install deps in a builder layer, copy only the app +
# installed packages into a slim runtime layer, run as a non-root user.
#
# Base image is pulled from the ECR Public mirror (public.ecr.aws) instead
# of docker.io directly — CodeBuild's anonymous docker.io pulls started
# hitting Docker Hub's rate limit (HTTP 429), which broke unrelated
# rebuilds. ECR Public mirrors the same official image with no such limit
# for AWS-originated pulls.

FROM public.ecr.aws/docker/library/python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM public.ecr.aws/docker/library/python:3.12-slim AS runtime

# Non-root user — never run the app as root inside the container
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /usr/sbin/nologin --create-home appuser

COPY --from=builder /install /usr/local

WORKDIR /app
COPY app/ ./app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENV=production

USER appuser

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8100/api/health', timeout=3).status==200 else sys.exit(1)"

# IMPORTANT: must stay at 1 worker (do NOT add --workers N > 1) as long as
# app/cmdb.py's fetch cache is a plain in-process module-level dict/lock.
# --workers spawns multiple independent OS processes that do NOT share
# Python memory — with 2+ workers, each ALB request could land on a
# different worker, each with its own empty/incomplete cache and its own
# _fetch_in_progress flag. In practice this caused the CMDB page to poll
# forever ("fetch_status": "pending", accounts: []) because a poll that
# hit worker B never saw the fetch that had already completed on worker
# A. If this ever needs multiple workers again, the CMDB cache must first
# be moved to a shared store (e.g. the DynamoDB tables cmdb_store.py
# already writes to) instead of process memory.
#
# The CPU-starvation issue this was originally meant to help with is
# instead addressed by: (1) app/cmdb.py running the org-wide scan on a
# background thread instead of the request thread, and (2) the Fargate
# task's CPU/memory being doubled (512->1024 / 1024->2048, see
# compute_stack.py) — both changes work fine with a single worker.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
