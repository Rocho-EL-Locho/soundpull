# syntax=docker/dockerfile:1

# ─── Stage 1: builder — install Python deps + app into a portable target dir ──
# Python 3.11 to match the distroless runtime (debian12 ships CPython 3.11).
FROM python:3.11-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml ./
COPY app ./app
# --no-compile + pruning bytecode keeps /install lean.
RUN pip install --no-compile --target=/install . \
 && find /install -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /install -name '*.pyc' -delete
# Pre-create writable runtime dirs owned by the distroless nonroot uid (65532).
RUN mkdir -p /runtime/data /runtime/downloads && chown -R 65532:65532 /runtime

# ─── Stage 2: static ffmpeg/ffprobe (yt-dlp needs them at runtime) ────────────
# Self-contained static binaries with full codec support — robust, no shared-lib
# or glibc-overlay concerns in the distroless runtime.
FROM mwader/static-ffmpeg:7.1 AS ffmpeg

# ─── Stage 2b: Deno JS runtime (yt-dlp EJS: solves YouTube signature/n challenges)
# The :bin flavor is just the deno binary at /deno. Without a JS runtime yt-dlp
# can no longer decipher YouTube formats ("Only images are available").
FROM denoland/deno:bin-2.9.1 AS deno

# ─── Stage 3: distroless runtime ──────────────────────────────────────────────
FROM gcr.io/distroless/python3-debian12:nonroot
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/install \
    PATH=/usr/bin:/bin

# Python dependencies + the app package
COPY --from=builder /install /install
# Static ffmpeg + ffprobe on PATH
COPY --from=ffmpeg /ffmpeg /usr/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/bin/ffprobe
# Deno JS runtime for yt-dlp's EJS challenge solver (YouTube signature/n challenges).
# DENO_DIR must be writable by the distroless nonroot uid — point it at /downloads.
COPY --from=deno /deno /usr/bin/deno
ENV DENO_DIR=/downloads/.deno
# Writable data + staging dirs, owned by nonroot
COPY --from=builder --chown=65532:65532 /runtime/data /data
COPY --from=builder --chown=65532:65532 /runtime/downloads /downloads

EXPOSE 8080
# Liveness probe. Distroless has no shell, so call the interpreter directly
# (HEALTHCHECK's exec form does not go through ENTRYPOINT).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/usr/bin/python3.11", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"]
# The distroless python3 image's entrypoint IS the interpreter, so CMD = args.
CMD ["-m", "app.main"]
