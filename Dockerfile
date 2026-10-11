FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CLIPSHIP_DATA_DIR=/data \
    HF_HOME=/data/huggingface \
    HOME=/tmp \
    IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 clipship \
    && useradd --system --uid 10001 --gid clipship --home-dir /nonexistent clipship \
    && mkdir -p /data \
    && chown clipship:clipship /data

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app.py captioning.py clip_factory.py engagement.py pipeline.py quota_budget.py trend_scout.py youtube_api.py container_entrypoint.py ./
COPY static ./static
COPY templates ./templates

USER clipship

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:5000/healthz', timeout=3)"

CMD ["python", "container_entrypoint.py"]
