FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates \
    libreoffice-writer-nogui libreoffice-java-common default-jre-headless \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
COPY --from=brainicism/bgutil-ytdlp-pot-provider:2.0.2 /usr/local/bin/node /usr/local/bin/node
COPY --from=brainicism/bgutil-ytdlp-pot-provider:2.0.2 /app /pot
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt
COPY app.py icons_data.py convert.py ./
COPY static ./static
CMD ["sh","-c","(cd /pot && node build/main.js >/tmp/pot.log 2>&1 &) ; uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
