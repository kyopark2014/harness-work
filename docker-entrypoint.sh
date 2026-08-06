#!/bin/sh
set -e

if [ -n "$APP_CONFIG_JSON" ]; then
  echo "$APP_CONFIG_JSON" > /app/application/config.json
fi

exec "$@"
