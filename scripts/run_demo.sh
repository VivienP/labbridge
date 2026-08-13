#!/bin/sh
set -eu

alembic upgrade head
exec uvicorn labbridge.api.demo:app --host 0.0.0.0 --port 8000
