#!/usr/bin/env bash
# scripts/e2e_http_registry.sh
#
# Location: ./scripts/e2e_http_registry.sh
# Copyright 2026
# SPDX-License-Identifier: Apache-2.0
#
# PR3 -- end-to-end verification of the HTTP Registry against a LIVE
# gateway (real HTTP, no TestClient): import OpenAPI → candidate →
# diff → preview → activate → tool sync → MCP tools/list + tools/call
# → health check → schema drift on a changed spec → clean logs.
#
#  1. Starts the stdlib petstore server (tests/http_test_server/server.py)
#     on 127.0.0.1:8899 and waits for it.
#  2. Waits for the gateway /health.
#  3. Mints an admin JWT with the CLI (mcpgateway.utils.create_jwt_token).
#  4. Creates an HTTP service via POST /admin/http.
#  5. Imports the v1 OpenAPI spec (activate=true) via
#     POST /admin/http/{id}/schemas/import (multipart) → 8 tools.
#  6. Asserts tools/list over JSON-RPC (/rpc) and invokes three of them
#     with tools/call: GET query, POST JSON, DELETE 204.
#  7. Runs the immediate health check (POST /admin/http/{id}/health).
#  8. Imports the v2 spec (activate=false) → candidate + schema_drift;
#     diff/preview, then activates and asserts the tool flip
#     (echo-form disabled, filter added).
#  9. Asserts no unexpected Traceback in the gateway container logs
#     (the known metrics-buffer/tool_metrics FK race -- pre-existing
#     product behaviour shared with gRPC tools -- is excluded).
# 10. Deletes the service (cascade) unless KEEP_SERVICE=1.
#
# Usage (against the 4444 container):
#   JWT_SECRET_KEY=<gateway-jwt-secret> scripts/e2e_http_registry.sh
#
# Env overrides:
#   GATEWAY_URL      default http://127.0.0.1:4444
#   PETSTORE_PORT    default 8899
#   KEEP_SERVICE=1   skip the final service deletion (debugging)
#   SKIP_PETSTORE=1  reuse an already-running petstore server
#   CONTAINER_NAME   default mcpgateway (for the log-scan step)
#   TEAM_IDS         comma-separated team IDs for the admin JWT (required:
#                    the admin gate denies tokens with an empty teams claim
#                    as public-only). Example: TEAM_IDS=team-a,team-b
#
# The JWT mint runs on the host, so BOTH gateway secrets must be exported
# (config.py rejects placeholder values otherwise):
#   JWT_SECRET_KEY + AUTH_ENCRYPTION_SECRET

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Config (override via env)
# ---------------------------------------------------------------------------
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:4444}"
PETSTORE_PORT="${PETSTORE_PORT:-8899}"
PETSTORE_URL="http://127.0.0.1:${PETSTORE_PORT}"
KEEP_SERVICE="${KEEP_SERVICE:-0}"
SKIP_PETSTORE="${SKIP_PETSTORE:-0}"
CONTAINER_NAME="${CONTAINER_NAME:-mcpgateway}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"

SERVICE_NAME="e2e-http-$$"
SPEC_TMP_DIR="$(mktemp -d -t e2e-http-registry.XXXXXX)"
PETSTORE_LOG="$SPEC_TMP_DIR/petstore.log"
PETSTORE_PID=""
SERVICE_ID=""

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
for bin in curl jq "$PYTHON_BIN"; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "❌ Required tool not found: $bin" >&2
    exit 1
  fi
done
if [[ -z "${JWT_SECRET_KEY:-}" ]]; then
  echo "❌ JWT_SECRET_KEY must be set to the gateway's signing secret" >&2
  exit 1
fi
if [[ -z "${TEAM_IDS:-}" ]]; then
  echo "❌ TEAM_IDS must be set (the admin gate denies tokens with an empty teams claim)" >&2
  exit 1
fi

cleanup() {
  if [[ -n "$SERVICE_ID" && "$KEEP_SERVICE" != "1" ]]; then
    echo "🧹 Deleting HTTP service $SERVICE_ID..."
    curl -fsS -X DELETE "$GATEWAY_URL/admin/http/$SERVICE_ID" -H "$AUTH_HEADER" >/dev/null 2>&1 || true
  fi
  if [[ -n "$PETSTORE_PID" ]]; then
    kill "$PETSTORE_PID" >/dev/null 2>&1 || true
    wait "$PETSTORE_PID" 2>/dev/null || true
  fi
  rm -rf "$SPEC_TMP_DIR"
}
trap cleanup EXIT

wait_for_http() {
  local url="$1" attempts="${2:-60}" delay="${3:-1}"
  local i
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

jq_assert() {
  # jq_assert <description> <jq-filter> <json>
  local description="$1" filter="$2" json="$3" value
  value="$(echo "$json" | jq -c -e "$filter" 2>/dev/null)" || {
    echo "❌ $description (jq filter failed: $filter)" >&2
    echo "$json" >&2
    exit 1
  }
  echo "✅ $description"
}

# ---------------------------------------------------------------------------
# Step 0: start the petstore server
# ---------------------------------------------------------------------------
if [[ "$SKIP_PETSTORE" == "1" ]]; then
  echo "ℹ️  SKIP_PETSTORE=1 -- reusing petstore server at $PETSTORE_URL"
else
  echo "🚀 Starting petstore test server on 127.0.0.1:$PETSTORE_PORT..."
  "$PYTHON_BIN" tests/http_test_server/server.py --port "$PETSTORE_PORT" >"$PETSTORE_LOG" 2>&1 &
  PETSTORE_PID=$!
  if ! wait_for_http "$PETSTORE_URL/health" 30; then
    echo "❌ Petstore server did not start within 30s" >&2
    exit 1
  fi
  echo "✅ Petstore server healthy"
fi

# ---------------------------------------------------------------------------
# Step 1: gateway health + admin JWT
# ---------------------------------------------------------------------------
if ! wait_for_http "$GATEWAY_URL/health" 30; then
  echo "❌ Gateway not reachable at $GATEWAY_URL" >&2
  exit 1
fi
echo "✅ Gateway /health reachable"

MINT_ARGS=(--username "$ADMIN_EMAIL" --secret "$JWT_SECRET_KEY" --exp 600 --admin)
if [[ -n "${TEAM_IDS:-}" ]]; then
  MINT_ARGS+=(--teams "$TEAM_IDS")
fi
TOKEN="$("$PYTHON_BIN" -m mcpgateway.utils.create_jwt_token "${MINT_ARGS[@]}" 2>/dev/null)"
AUTH_HEADER="Authorization: Bearer $TOKEN"
echo "✅ Admin JWT minted for $ADMIN_EMAIL"

# ---------------------------------------------------------------------------
# Step 2: create the HTTP service
# ---------------------------------------------------------------------------
CREATE_RESPONSE=$(curl -fsS -X POST "$GATEWAY_URL/admin/http" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d "{\"name\": \"$SERVICE_NAME\", \"base_url\": \"$PETSTORE_URL\", \"visibility\": \"public\"}")
SERVICE_ID="$(echo "$CREATE_RESPONSE" | jq -r '.id')"
echo "✅ HTTP service created: $SERVICE_NAME ($SERVICE_ID)"

# ---------------------------------------------------------------------------
# Step 3: import v1 spec (activate=true) → 8 tools
# ---------------------------------------------------------------------------
curl -fsS "$PETSTORE_URL/openapi.json" -o "$SPEC_TMP_DIR/openapi-v1.json"
IMPORT_V1=$(curl -fsS -X POST "$GATEWAY_URL/admin/http/$SERVICE_ID/schemas/import" \
  -H "$AUTH_HEADER" \
  -F "artifact=@$SPEC_TMP_DIR/openapi-v1.json;filename=openapi.json" \
  -F "activate=true")
ARTIFACT_V1="$(echo "$IMPORT_V1" | jq -r '.id')"
jq_assert "v1 artifact imported and active" '.is_active == true' "$IMPORT_V1"

TOOLS_LIST=$(curl -fsS -X POST "$GATEWAY_URL/rpc" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": "e2e-http-tools-list", "method": "tools/list", "params": {}}')
# Scope to this run's service: the live gateway may already host tools
# from other integrations (gRPC/SQL), so global counts are unreliable.
jq_assert "tools/list shows 8 registry tools for $SERVICE_NAME" \
  ".result.tools | map(select(.name | contains(\"$SERVICE_NAME\"))) | length == 8" "$TOOLS_LIST"
for needle in listpets createpet deletepet echoform; do
  jq_assert "tools/list contains $needle" \
    ".result.tools | map(select(.name | contains(\"$SERVICE_NAME\"))) | map(.name) | any(contains(\"$needle\"))" "$TOOLS_LIST"
done

# ---------------------------------------------------------------------------
# Step 4: MCP tools/call — GET query, POST JSON, DELETE 204
# ---------------------------------------------------------------------------
LISTPETS_NAME="$(echo "$TOOLS_LIST" | jq -r ".result.tools[] | select(.name | contains(\"$SERVICE_NAME\")) | select(.name | contains(\"listpets\")) | .name" | head -1)"
CREATEPET_NAME="$(echo "$TOOLS_LIST" | jq -r ".result.tools[] | select(.name | contains(\"$SERVICE_NAME\")) | select(.name | contains(\"createpet\")) | .name" | head -1)"
DELETEPET_NAME="$(echo "$TOOLS_LIST" | jq -r ".result.tools[] | select(.name | contains(\"$SERVICE_NAME\")) | select(.name | contains(\"deletepet\")) | .name" | head -1)"

RPC_CALL=$(curl -fsS -X POST "$GATEWAY_URL/rpc" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d "{\"jsonrpc\": \"2.0\", \"id\": \"e2e-http-call-1\", \"method\": \"tools/call\", \"params\": {\"name\": \"$LISTPETS_NAME\", \"arguments\": {\"query\": {\"limit\": 5}}}}")
jq_assert "GET /v1/pets returns Fido" '.result.content[0].text | contains("Fido")' "$RPC_CALL"

RPC_CALL=$(curl -fsS -X POST "$GATEWAY_URL/rpc" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d "{\"jsonrpc\": \"2.0\", \"id\": \"e2e-http-call-2\", \"method\": \"tools/call\", \"params\": {\"name\": \"$CREATEPET_NAME\", \"arguments\": {\"body\": {\"name\": \"E2ERex\", \"tag\": \"e2e\"}}}}")
jq_assert "POST /v1/pets creates E2ERex" '.result.content[0].text | contains("E2ERex")' "$RPC_CALL"

RPC_CALL=$(curl -fsS -X POST "$GATEWAY_URL/rpc" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d "{\"jsonrpc\": \"2.0\", \"id\": \"e2e-http-call-3\", \"method\": \"tools/call\", \"params\": {\"name\": \"$DELETEPET_NAME\", \"arguments\": {\"path\": {\"petId\": 1}}}}")
jq_assert "DELETE /v1/pets/{petId} returns 204 No Content" '.result.content[0].text | contains("No Content")' "$RPC_CALL"

# ---------------------------------------------------------------------------
# Step 5: immediate health check
# ---------------------------------------------------------------------------
HEALTH_RESPONSE=$(curl -fsS -X POST "$GATEWAY_URL/admin/http/$SERVICE_ID/health" -H "$AUTH_HEADER")
jq_assert "health check reports healthy" '.healthy == true' "$HEALTH_RESPONSE"
# reachability lives on the service read model, not the check-result payload.
SERVICE_READ=$(curl -fsS "$GATEWAY_URL/admin/http/$SERVICE_ID" -H "$AUTH_HEADER")
jq_assert "health check marks the service reachable" '.reachable == true' "$SERVICE_READ"

# ---------------------------------------------------------------------------
# Step 6: v2 drift → candidate → diff → preview → activate
# ---------------------------------------------------------------------------
curl -fsS "$PETSTORE_URL/openapi-v2.json" -o "$SPEC_TMP_DIR/openapi-v2.json"
IMPORT_V2=$(curl -fsS -X POST "$GATEWAY_URL/admin/http/$SERVICE_ID/schemas/import" \
  -H "$AUTH_HEADER" \
  -F "artifact=@$SPEC_TMP_DIR/openapi-v2.json;filename=openapi-v2.json" \
  -F "activate=false")
ARTIFACT_V2="$(echo "$IMPORT_V2" | jq -r '.id')"
jq_assert "v2 artifact imported as inactive" '.is_active == false' "$IMPORT_V2"

SERVICE_READ=$(curl -fsS "$GATEWAY_URL/admin/http/$SERVICE_ID" -H "$AUTH_HEADER")
jq_assert "schema_drift set on changed spec" '.schema_drift == true' "$SERVICE_READ"

DIFF_RESPONSE=$(curl -fsS "$GATEWAY_URL/admin/http/$SERVICE_ID/schemas/diff?from=$ARTIFACT_V1&to=$ARTIFACT_V2" -H "$AUTH_HEADER")
jq_assert "diff adds GET /v1/pets/filter" '.added_operations | index("GET /v1/pets/filter") != null' "$DIFF_RESPONSE"
jq_assert "diff removes POST /v1/pets/echo-form" '.removed_operations | index("POST /v1/pets/echo-form") != null' "$DIFF_RESPONSE"

PREVIEW_RESPONSE=$(curl -fsS "$GATEWAY_URL/admin/http/$SERVICE_ID/schemas/$ARTIFACT_V2/preview" -H "$AUTH_HEADER")
jq_assert "preview adds filter tool" '.added_tools | index("GET /v1/pets/filter") != null' "$PREVIEW_RESPONSE"
jq_assert "preview disables echo-form tool" '.disabled_tools | index("POST /v1/pets/echo-form") != null' "$PREVIEW_RESPONSE"

ACTIVATE_RESPONSE=$(curl -fsS -X POST "$GATEWAY_URL/admin/http/$SERVICE_ID/schemas/$ARTIFACT_V2/activate" -H "$AUTH_HEADER")
jq_assert "v2 activation converges the active pointer" '.is_active == true' "$ACTIVATE_RESPONSE"

SERVICE_READ=$(curl -fsS "$GATEWAY_URL/admin/http/$SERVICE_ID" -H "$AUTH_HEADER")
jq_assert "schema_drift cleared after activation" '.schema_drift == false' "$SERVICE_READ"

TOOLS_LIST=$(curl -fsS -X POST "$GATEWAY_URL/rpc" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": "e2e-http-tools-list-2", "method": "tools/list", "params": {}}')
jq_assert "tools/list shows the new filter tool" \
  ".result.tools | map(select(.name | contains(\"$SERVICE_NAME\"))) | map(.name) | any(contains(\"filterpets\"))" "$TOOLS_LIST"
jq_assert "tools/list no longer lists echo-form" \
  ".result.tools | map(select(.name | contains(\"$SERVICE_NAME\"))) | map(.name) | all(contains(\"echoform\") | not)" "$TOOLS_LIST"

# ---------------------------------------------------------------------------
# Step 7: gateway logs must be clean
# ---------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^$CONTAINER_NAME$"; then
  # Exclude only the known metrics-buffer/tool_metrics FK race: buffered
  # metric rows can hit a FOREIGN KEY failure when the E2E service is
  # cascade-deleted before the flush lands (pre-existing product
  # behaviour, also affects gRPC tools).  Every other traceback fails.
  # NOTE: use -c (not a heredoc) — a heredoc would override python's
  # stdin and SIGPIPE docker logs, aborting the run under pipefail.
  BAD_TRACEBACKS=$(docker logs "$CONTAINER_NAME" --since 5m 2>&1 | "$PYTHON_BIN" -c '
import sys

KNOWN_RACE = ("Failed to flush tool metrics to database", "FOREIGN KEY constraint failed")
bad = [line for line in sys.stdin if "Traceback" in line and not all(m in line for m in KNOWN_RACE)]
print(len(bad))
for line in bad[:5]:
    print(line[:240])
')
  BAD_COUNT="$(echo "$BAD_TRACEBACKS" | head -1)"
  if [[ "$BAD_COUNT" -eq 0 ]]; then
    echo "✅ No unexpected Traceback in $CONTAINER_NAME logs (last 5m)"
  else
    echo "❌ Found $BAD_COUNT unexpected Traceback(s) in $CONTAINER_NAME logs" >&2
    echo "$BAD_TRACEBACKS" | tail -n +2 >&2
    exit 1
  fi
else
  echo "ℹ️  Skipping container log check ($CONTAINER_NAME not running / docker unavailable)"
fi

echo "🎉 E2E HTTP Registry verification passed"
