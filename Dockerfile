ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PADDLE_PDX_CACHE_HOME=/opt/paddlex \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
ARG APT_MIRROR_URL=https://ftp.debian.org/debian/
ARG APT_SECURITY_MIRROR_URL=https://security.debian.org/debian-security/

# Debian's HTTPS servers by default; CI overrides both with the internal mirror.
# The shell fallbacks cover CI passing an unset variable as an empty build arg.
RUN APT_MIRROR_URL="${APT_MIRROR_URL:-https://ftp.debian.org/debian/}" && \
    APT_SECURITY_MIRROR_URL="${APT_SECURITY_MIRROR_URL:-https://security.debian.org/debian-security/}" && \
    . /etc/os-release && \
    printf '%s\n' \
        'Types: deb' \
        "URIs: ${APT_MIRROR_URL}" \
        "Suites: ${VERSION_CODENAME} ${VERSION_CODENAME}-updates" \
        'Components: main contrib non-free non-free-firmware' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        '' \
        'Types: deb' \
        "URIs: ${APT_SECURITY_MIRROR_URL}" \
        "Suites: ${VERSION_CODENAME}-security" \
        'Components: main contrib non-free non-free-firmware' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        > /etc/apt/sources.list.d/debian.sources
# System libraries needed by OpenCV / Paddle on a slim image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*


FROM base AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements.txt \
    && pip uninstall -y pip


FROM builder AS models
WORKDIR /build
# Only the modules load_ocr() imports, so editing the bot does not re-download the models.
COPY pixel2num/__init__.py pixel2num/ranges.py pixel2num/extract_numbers.py ./pixel2num/
RUN python -c "from pixel2num.extract_numbers import load_ocr; load_ocr()" \
    && chmod -R a+rX /opt/paddlex


FROM base AS runtime
LABEL org.opencontainers.image.authors="ali99kalbasi82@gmail.com, kian1abdipour@gmail.com" \
      org.opencontainers.image.title="pixel2num-bot" \
      org.opencontainers.image.description="Mattermost bot that OCRs top-up card photos into an Excel report"
ENV DATA_DIR=/app/data
RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /app/data \
    && chown bot:bot /app/data
WORKDIR /app
# Everything outside /app/data stays root-owned and read-only for the bot user.
COPY --from=builder /opt/venv /opt/venv
COPY --from=models /opt/paddlex /opt/paddlex
COPY pixel2num ./pixel2num
USER 10001:10001
VOLUME ["/app/data"]
CMD ["python", "-m", "pixel2num"]
