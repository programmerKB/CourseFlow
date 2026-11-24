FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home app
# Voice input transcribes on this server with faster-whisper. The model is downloaded while the
# image is built, so the running container needs no Internet access (HF_HUB_OFFLINE=1).
# Build with WHISPER_MODEL=small for better accuracy if the host has about 1 GB more memory.
ARG WHISPER_MODEL=base
ENV WHISPER_MODEL=${WHISPER_MODEL} WHISPER_MODEL_DIR=/opt/whisper HF_HUB_DISABLE_TELEMETRY=1
RUN python -c "import os; from faster_whisper.utils import download_model; \
download_model(os.environ['WHISPER_MODEL'], cache_dir=os.environ['WHISPER_MODEL_DIR'])" \
    && chown -R app:app /opt/whisper
ENV HF_HUB_OFFLINE=1
COPY . .
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
