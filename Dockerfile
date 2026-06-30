# syntax=docker/dockerfile:1

# ─── Stage 1: builder — install Python deps + app into a portable target dir ──
# Python 3.11 to match the distroless runtime (debian12 ships CPython 3.11).
FROM python:3.11-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml ./
COPY app ./app
# Install into /install (no venv) so it can be copied into distroless as-is.
RUN pip install --target=/install .
# Pre-create writable runtime dirs owned by the distroless nonroot uid (65532).
RUN mkdir -p /runtime/data /runtime/downloads && chown -R 65532:65532 /runtime

# ─── Stage 2: static ffmpeg/ffprobe (yt-dlp needs them at runtime) ────────────
FROM mwader/static-ffmpeg:7.1 AS ffmpeg

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
# Writable data + staging dirs, owned by nonroot
COPY --from=builder --chown=65532:65532 /runtime/data /data
COPY --from=builder --chown=65532:65532 /runtime/downloads /downloads

EXPOSE 8080
# The distroless python3 image's entrypoint IS the interpreter, so CMD = args.
CMD ["-m", "app.main"]
