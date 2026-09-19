# Sales Coach, hosted: one container, one seller, one volume at /data. See docs/deploy.md.
#
#   docker build -t salescoach .
#   docker run --rm -p 127.0.0.1:8140:8140 -v salescoach-data:/data \
#       -e SALESCOACH_PASSWORD=... -e SALESCOACH_PUBLIC_URL=http://127.0.0.1:8140 salescoach
#
# The package is installed editable from this checkout (/app), because config/ and the other
# folders beside the package are found relative to it, exactly as on a laptop. Everything the app
# writes goes under /data (SALESCOACH_DATA) and /data/runtime (SALESCOACH_RUNTIME): mount one
# volume there. ffmpeg is for audio uploads; there is no local transcription in the image.
#
# The process runs as the unprivileged user `salescoach` (uid 1000). The image starts as root only
# for docker-entrypoint.sh to make /data writable by that user (platforms mount volumes owned by
# root) and then drops privileges for good. If the platform runs the container as a non-root user
# itself, the entrypoint skips the chown and just execs.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SALESCOACH_DATA=/data \
    SALESCOACH_RUNTIME=/data/runtime \
    PORT=8140

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 salescoach \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin salescoach \
    && mkdir -p /data \
    && chown salescoach:salescoach /data

WORKDIR /app
COPY --chown=salescoach:salescoach . /app
RUN pip install -e /app \
    && chmod 0755 /app/docker-entrypoint.sh \
    && chown -R salescoach:salescoach /app/salescoach.egg-info

EXPOSE 8140
VOLUME ["/data"]

# python:slim has no curl; urllib is enough. The check follows PORT so a platform that sets it still works.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8140'), timeout=4).status == 200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec salescoach serve --host 0.0.0.0 --port ${PORT:-8140}"]
