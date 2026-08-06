# Stage 1: frontend build
FROM node:22-alpine AS frontend
WORKDIR /web
COPY application/web/package.json application/web/package-lock.json ./
RUN npm ci
COPY application/web/ .
RUN npm run build

# Stage 2: Python runtime
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirement.txt .
RUN pip install --upgrade "setuptools>=83.0.0" \
    && pip install --no-cache-dir -r requirement.txt

COPY . .
COPY --from=frontend /web/dist /app/application/web/dist

RUN chmod +x /app/docker-entrypoint.sh \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/api/health

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "application.server:app", "--host", "0.0.0.0", "--port", "8501", "--no-server-header"]
