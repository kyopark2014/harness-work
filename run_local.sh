#!/usr/bin/env bash
# Local build + run: frontend (npm) then FastAPI (uvicorn) on :8501
# If port 8501 is already in use, the existing process is stopped first.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8501

cd "$ROOT"

echo "==> Frontend build (application/web)"
cd application/web
npm install
npm run build
cd "$ROOT"

echo "==> Freeing port ${PORT} if occupied"
if command -v lsof >/dev/null 2>&1; then
  PIDS="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${PIDS}" ]]; then
    echo "    Port ${PORT} in use by PID(s): ${PIDS} — killing"
    # shellcheck disable=SC2086
    kill ${PIDS} 2>/dev/null || true
    sleep 1
    PIDS="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "${PIDS}" ]]; then
      # shellcheck disable=SC2086
      kill -9 ${PIDS} 2>/dev/null || true
      sleep 0.5
    fi
  else
    echo "    Port ${PORT} is free"
  fi
else
  echo "    lsof not found; skipping port check"
fi

# Task store = DynamoDB. Locally use DynamoDB Local (tables are auto-created on startup).
# Set DYNAMODB_ENDPOINT_URL yourself to use another endpoint (or unset + TASK_STORE_AUTO_CREATE=1
# with real AWS credentials to use the AWS tables).
if [[ -z "${DYNAMODB_ENDPOINT_URL:-}" ]]; then
  DDB_PORT="${DDB_LOCAL_PORT:-8000}"
  DDB_NAME="harness-work-dynamodb-local"
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required to run DynamoDB Local (or set DYNAMODB_ENDPOINT_URL)" >&2
    exit 1
  fi
  if [[ "$(docker inspect -f '{{.State.Running}}' "${DDB_NAME}" 2>/dev/null || true)" != "true" ]]; then
    echo "==> Starting DynamoDB Local on :${DDB_PORT} (data: application/data/dynamodb)"
    mkdir -p "${ROOT}/application/data/dynamodb"
    docker rm -f "${DDB_NAME}" >/dev/null 2>&1 || true
    docker run -d --name "${DDB_NAME}" -u root -p "${DDB_PORT}:8000" \
      -v "${ROOT}/application/data/dynamodb:/home/dynamodblocal/data" \
      amazon/dynamodb-local:latest -jar DynamoDBLocal.jar -sharedDb -dbPath /home/dynamodblocal/data >/dev/null
    sleep 2
  else
    echo "==> DynamoDB Local already running (${DDB_NAME})"
  fi
  export DYNAMODB_ENDPOINT_URL="http://localhost:${DDB_PORT}"
fi
echo "    DynamoDB endpoint: ${DYNAMODB_ENDPOINT_URL}"

echo "==> Starting uvicorn on 0.0.0.0:${PORT}"
echo "    Open http://localhost:${PORT}"
# Local-only: Swagger UI at /docs (disabled by default in production/ECS).
export ENABLE_API_DOCS="${ENABLE_API_DOCS:-1}"

# Prefer python3.13: PATH uvicorn may resolve to Homebrew Python 3.14,
# while project deps (pip3) are typically installed for 3.13.
if command -v python3.13 >/dev/null 2>&1; then
  PYTHON=(python3.13)
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=(python3)
else
  PYTHON=(python)
fi
echo "    Using: ${PYTHON[*]} ($(${PYTHON[*]} -c 'import sys; print(sys.version.split()[0])'))"
exec "${PYTHON[@]}" -m uvicorn application.server:app --host 0.0.0.0 --port "${PORT}" --no-server-header
