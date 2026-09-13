FROM python:3.12-slim-bookworm AS models
WORKDIR /build
COPY worker/download_models.py .
RUN python download_models.py --output /models

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MODEL_DIR=/models INFERENCE_DEVICE=cpu OPENCV_LOG_LEVEL=SILENT
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY worker/requirements-common.txt worker/requirements.txt /tmp/requirements/
RUN pip install --no-cache-dir -r /tmp/requirements/requirements.txt
COPY --from=models /models /models
COPY worker/*.py /app/worker/
USER 65532:65532
CMD ["python", "-m", "worker.main"]
