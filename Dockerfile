# StudyMate — production image
# One container serves the API and the static front end. Works on Hugging Face
# Spaces (expects 7860), Render/Railway/Fly (they inject $PORT) and local Docker.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860 \
    STUDYMATE_DATA=/data

WORKDIR /srv

# PyMuPDF ships manylinux wheels, so no compiler or system libs are needed.
COPY app/requirements.txt ./app/requirements.txt
RUN pip install --no-cache-dir -r app/requirements.txt huggingface_hub

COPY app ./app

RUN mkdir -p /data && useradd -m -u 1000 studymate && chown -R studymate:studymate /data /srv
USER studymate

EXPOSE 7860
HEALTHCHECK --interval=45s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import os,urllib.request as u;u.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"7860\")}/healthz',timeout=4)"

# --proxy-headers so the app builds correct absolute URLs behind a proxy/CDN
CMD ["sh", "-c", "cd app && exec python -m uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
