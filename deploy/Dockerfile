FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 update \
    && apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 install -y --no-install-recommends libopus0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd -g 10001 english_edge && useradd -u 10001 -g english_edge -M english_edge
FROM base AS dependencies
COPY wheels/ /wheels/
RUN pip install --no-index --find-links=/wheels --require-hashes --prefix=/install -r /wheels/linux.lock
FROM base
WORKDIR /app
COPY --from=dependencies /install /usr/local
COPY requirements-cloud.lock ./
COPY scripts/ ./scripts/
COPY debug/ ./debug/
COPY upstream/backend/main/xiaozhi-server/ ./upstream/backend/main/xiaozhi-server/
# Release extraction uses a private umask. Application code must be readable
# by the unprivileged runtime user; secrets are never part of /app.
RUN chmod -R a+rX /app
USER 10001:10001
CMD ["python", "scripts/run_cloud.py", "--env-file", "/run/secrets/cloud.env", "--devices-file", "/run/secrets/devices.json", "--admin-file", "/run/secrets/admin.json", "--host", "0.0.0.0"]
