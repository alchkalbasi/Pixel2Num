FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data \
    PADDLE_PDX_CACHE_HOME=/opt/paddlex PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
WORKDIR /app
# System libraries needed by OpenCV / Paddle on a slim image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home bot \
    && mkdir /app/data && chown bot:bot /app/data
COPY pixel2num ./pixel2num
# Download the OCR models now, so the running container needs no internet access (SPEC R-OCR-2).
RUN python -c "from pixel2num.extract_numbers import load_ocr; load_ocr()" \
    && chmod -R a+rX /opt/paddlex
USER bot
CMD ["python", "-m", "pixel2num"]
