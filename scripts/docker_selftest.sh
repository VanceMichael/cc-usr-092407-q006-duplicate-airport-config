#!/usr/bin/env bash
# End-to-end Docker self-test for the airport disruption service.
#
# In a clean environment this script:
#   1. builds the business image from Dockerfile (no cache assumptions),
#   2. starts the api service via docker compose with a fresh named volume,
#   3. runs the in-container test suite (unit tests),
#   4. seeds valid + invalid + idempotent + cross-midnight events over HTTP,
#   5. restarts the container and verifies persistence and replay,
#   6. proves a reordered airport file converges on the same definition,
#   7. proves a conflicting instance exits at the startup gate while the old
#      instance stays readable,
#   8. tears everything down (volume included).
#
# It never reaches any external flight or map service: all traffic is between
# the script and the local container endpoint.
#
# Usage: scripts/docker_selftest.sh
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT="disrupt-selftest"
COMPOSE=(docker compose -p "$PROJECT" -f compose.yaml)

log() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nSELF-TEST FAILED: %s\n' "$*" >&2; exit 1; }

cleanup() {
    log "Teardown: stopping containers and deleting the test volume"
    docker rm -f disrupt-selftest-conflict >/dev/null 2>&1 || true
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

command -v docker >/dev/null 2>&1 || fail "docker is required but was not found"
docker compose version >/dev/null 2>&1 || fail "docker compose v2 is required"

# 1. Clean slate + fresh build ------------------------------------------------
log "Clean slate: removing any previous $PROJECT project resources"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true

log "Building image from Dockerfile"
"${COMPOSE[@]}" build --no-cache api

# 2. Start and wait for health ------------------------------------------------
log "Starting api service with a fresh named volume"
"${COMPOSE[@]}" up -d --wait api

# 3. Unit tests inside the image ---------------------------------------------
log "Running automated unit tests inside the image"
"${COMPOSE[@]}" exec -T api python -m unittest discover -s tests -v

# 4. HTTP seed phase ----------------------------------------------------------
log "Seed phase: valid/invalid events, idempotency, cross-midnight"
"${COMPOSE[@]}" exec -T api python scripts/selftest_client.py seed http://127.0.0.1:8080

# Sanity: the SQLite file really lives on the mounted volume.
log "Checking database file exists on the persistent volume"
"${COMPOSE[@]}" exec -T api sh -c 'test -s /data/disruptions.db && echo "db file present"'

# 5. Restart container, then verify -------------------------------------------
log "Restarting api container (volume stays attached)"
"${COMPOSE[@]}" restart api

log "Waiting for the restarted service to become healthy"
deadline=$(( $(date +%s) + 60 ))
container_id="$("${COMPOSE[@]}" ps -q api)"
while :; do
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || echo starting)"
    [ "$health" = "healthy" ] && break
    [ "$(date +%s)" -ge "$deadline" ] && fail "service did not become healthy after restart (last state: $health)"
    sleep 1
done
echo "container is healthy after restart"

log "Verify phase: persistence after restart + idempotent replay"
"${COMPOSE[@]}" exec -T api python scripts/selftest_client.py verify http://127.0.0.1:8080

# 6. Also validate that a fresh container against the SAME volume keeps data --
log "Recreating the api container against the same named volume"
"${COMPOSE[@]}" up -d --force-recreate --wait api
"${COMPOSE[@]}" exec -T api python scripts/selftest_client.py verify http://127.0.0.1:8080

# 7. Reordered airport records must converge on the same definition ---------
log "Reordering airports.json records in place (same definitions, new order)"
"${COMPOSE[@]}" exec -T api python - <<'PY'
import json
path = "/srv/fixtures/airports.json"
records = json.load(open(path))
records.reverse()
json.dump(records, open(path, "w"), ensure_ascii=False, indent=2)
print("airports.json now lists", [r["code"] for r in records])
PY

log "Restarting api with the reordered file; the digest must be unchanged"
digest_before="$("${COMPOSE[@]}" exec -T api python -c \
    "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/readyz'))['config_digest'])")"
"${COMPOSE[@]}" restart api >/dev/null
deadline=$(( $(date +%s) + 60 ))
container_id="$("${COMPOSE[@]}" ps -q api)"
while :; do
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || echo starting)"
    [ "$health" = "healthy" ] && break
    [ "$(date +%s)" -ge "$deadline" ] && fail "service did not become healthy with reordered fixtures (last state: $health)"
    sleep 1
done
digest_after="$("${COMPOSE[@]}" exec -T api python -c \
    "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/readyz'))['config_digest'])")"
[ "$digest_before" = "$digest_after" ] \
    || fail "config digest changed after reordering records: $digest_before -> $digest_after"
echo "config digest stable across record reordering: $digest_after"
"${COMPOSE[@]}" exec -T api python scripts/selftest_client.py verify http://127.0.0.1:8080

# 8. A conflicting instance must be refused while the old one serves --------
log "Preparing a conflicting airports.json (APS timezone changed)"
conflict_dir="$(mktemp -d)"
docker run --rm -i -v "$conflict_dir:/out" airport-disruption-api:local python - <<'PY'
import json, shutil
records = json.load(open("/srv/fixtures/airports.json"))
for record in records:
    if record["code"] == "APS":
        record["timezone"] = "America/New_York"
json.dump(records, open("/out/airports.json", "w"), ensure_ascii=False, indent=2)
shutil.copy("/srv/fixtures/flights.json", "/out/flights.json")
PY

log "Starting a throwaway instance with the conflicting config on the same volume"
conflict_container="$(docker run -d \
    --name disrupt-selftest-conflict \
    -e FIXTURES_DIR=/srv/fixtures-conflict \
    -e DB_PATH=/data/disruptions.db \
    -v "$conflict_dir:/srv/fixtures-conflict:ro" \
    -v "${PROJECT}_disruption-data:/data" \
    airport-disruption-api:local)"
rc="$(timeout 60 docker wait "$conflict_container" || echo timeout)"
[ "$rc" = "3" ] || fail "conflicting instance exit code '$rc', expected 3 (config conflict)"
docker logs "$conflict_container" 2>&1 | grep -q "airport_definition_changed" \
    || fail "conflicting instance did not report the changed airport definition"
docker rm -f "$conflict_container" >/dev/null 2>&1 || true
echo "conflicting instance refused with exit code 3 and a deterministic error"

log "Verifying the original instance is untouched and still serving"
"${COMPOSE[@]}" exec -T api python scripts/selftest_client.py verify http://127.0.0.1:8080
digest_final="$("${COMPOSE[@]}" exec -T api python -c \
    "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/readyz'))['config_digest'])")"
[ "$digest_final" = "$digest_after" ] \
    || fail "config digest changed after the refused upgrade attempt"
rm -rf "$conflict_dir"

# trap runs the teardown ------------------------------------------------------
trap - EXIT
cleanup
printf '\nALL DOCKER SELF-TESTS PASSED\n'
