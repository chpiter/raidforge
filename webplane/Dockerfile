# syntax=docker/dockerfile:1

FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEBPLANE_DATA_DIR=/data \
    WEBPLANE_HOST=0.0.0.0 \
    WEBPLANE_PORT=8080

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./webplane
COPY --from=web /web/dist ./webplane/web/dist

RUN useradd --system --create-home --uid 10001 webplane \
    && mkdir -p /data \
    && chown -R webplane:webplane /data /app
USER webplane

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/cp/health').status==200 else 1)"

CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8080", "webplane.app:app"]
