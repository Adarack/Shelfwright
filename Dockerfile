# postgres:18-alpine gives psql/pg_dump matching Chaptarr's PostgreSQL 18; the server itself is never started
FROM postgres:18-alpine
LABEL org.opencontainers.image.source="https://github.com/Adarack/Shelfwright" \
      org.opencontainers.image.description="Audiobook library upkeep for Audiobookshelf (and optionally Chaptarr)" \
      org.opencontainers.image.title="Shelfwright" \
      org.opencontainers.image.licenses="MIT"
ARG APP_VERSION=dev
LABEL org.opencontainers.image.version="${APP_VERSION}"
RUN apk add --no-cache python3 py3-mutagen tzdata ffmpeg
WORKDIR /app
COPY app/ /app/
RUN python3 -m py_compile /app/*.py && rm -rf /app/__pycache__
ENV PYTHONUNBUFFERED=1 PORT=8484 DATA_DIR=/data APP_VERSION=${APP_VERSION}
EXPOSE 8484
VOLUME /data
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:8484/api/summary >/dev/null || exit 1
ENTRYPOINT []
CMD ["python3", "/app/server.py"]
