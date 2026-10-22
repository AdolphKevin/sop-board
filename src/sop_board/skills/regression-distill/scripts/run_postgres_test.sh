#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 || ${1:-} == "--help" ]]; then
  echo "usage: SOP_DB_ENV=TEST_POSTGRES_DSN $0 <test command> [args...]"
  exit $([[ $# -eq 0 ]] && echo 2 || echo 0)
fi

db_env=${SOP_DB_ENV:-TEST_POSTGRES_DSN}
if [[ -n ${!db_env:-} ]]; then
  "$@"
  exit
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "error: $db_env is unset and Docker is unavailable" >&2
  exit 1
fi

image=${SOP_POSTGRES_IMAGE:-postgres:16-alpine}
container=$(docker run -d --rm \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=sop_test \
  -p 127.0.0.1::5432 \
  "$image")

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

port=$(docker port "$container" 5432/tcp | sed 's/.*://')
ready=false
for _ in $(seq 1 30); do
  if docker exec "$container" pg_isready -U postgres >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done

if [[ $ready != true ]]; then
  echo "error: PostgreSQL did not become ready" >&2
  exit 1
fi

dsn="host=127.0.0.1 port=${port} user=postgres password=postgres dbname=sop_test sslmode=disable"
url="postgres://postgres:postgres@127.0.0.1:${port}/sop_test?sslmode=disable"
if [[ $db_env == DATABASE_URL ]]; then
  export "$db_env=$url"
else
  export "$db_env=$dsn"
fi
export TEST_POSTGRES_DSN=${TEST_POSTGRES_DSN:-$dsn}
export DATABASE_URL=${DATABASE_URL:-$url}

"$@"
