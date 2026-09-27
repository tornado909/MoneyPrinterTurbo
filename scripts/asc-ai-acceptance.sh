#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${MPT_BASE_URL:-http://127.0.0.1:8080}"
API_KEY="${MPT_API_KEY:-}"
ACCEPT_RENDER="${MPT_ACCEPT_RENDER:-0}"
POLL_SECONDS="${MPT_ACCEPT_POLL_SECONDS:-5}"
RENDER_TIMEOUT="${MPT_ACCEPT_RENDER_TIMEOUT:-1800}"
TOPIC="${MPT_ACCEPT_TOPIC:-Как современные теплицы управляют светом и микроклиматом}"
IDEMPOTENCY_KEY="${MPT_ACCEPT_IDEMPOTENCY_KEY:-asc-ai-acceptance-v1}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

headers=(-H "Content-Type: application/json")
if [[ -n "$API_KEY" ]]; then
  headers+=(-H "x-api-key: $API_KEY")
fi

fail() {
  echo "ACCEPTANCE FAILED: $*" >&2
  exit 1
}

json_assert() {
  local file="$1"
  local expr="$2"
  python3 - "$file" "$expr" <<'PY'
import json, sys
path, expr = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as handle:
    data = json.load(handle)
env = {"data": data}
if not eval(expr, {"__builtins__": {}}, env):
    raise SystemExit(f"assertion failed: {expr}")
PY
}

json_value() {
  local file="$1"
  local path="$2"
  python3 - "$file" "$path" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    value = json.load(handle)
for part in sys.argv[2].split("."):
    if part:
        value = value[part]
print(value)
PY
}

echo "[1/6] Validating Docker Compose overlays..."
cd "$ROOT"
docker compose \
  -f docker-compose.yml \
  -f docker-compose.asc-ai.yml \
  -f docker-compose.chatterbox.yml \
  config -q || fail "docker compose config is invalid"

echo "[2/6] Checking ASC-AI capabilities..."
curl -fsS "${headers[@]}" "$BASE_URL/api/v1/asc-ai/capabilities" \
  -o "$TMP_DIR/capabilities.json" || fail "capabilities endpoint unavailable"
json_assert "$TMP_DIR/capabilities.json" 'data["status"] == 200'
json_assert "$TMP_DIR/capabilities.json" 'data["data"]["local_only"] is True'
json_assert "$TMP_DIR/capabilities.json" 'data["data"]["director"]["scheduler_managed"] is True'
json_assert "$TMP_DIR/capabilities.json" 'data["data"]["production"]["uses_shared_task_manager"] is True'

echo "[3/6] Checking local production dependencies and Russian Chatterbox voice..."
curl -fsS "${headers[@]}" "$BASE_URL/api/v1/asc-ai/health" \
  -o "$TMP_DIR/health.json" || fail "ASC-AI health/preflight failed"
json_assert "$TMP_DIR/health.json" 'data["status"] == 200'
json_assert "$TMP_DIR/health.json" 'data["data"]["scheduler"] is not None'
json_assert "$TMP_DIR/health.json" 'data["data"]["prompt_llm"] is not None'
json_assert "$TMP_DIR/health.json" 'data["data"]["chatterbox_voice"]["language"] == "ru"'

echo "[4/6] Running scheduler-managed Director planning smoke test..."
python3 - "$TOPIC" >"$TMP_DIR/plan-request.json" <<'PY'
import json, sys
print(json.dumps({
    "video_subject": sys.argv[1],
    "video_language": "ru-RU",
    "video_aspect": "9:16",
    "target_duration_seconds": 15,
    "max_local_video_scenes": 0,
    "max_public_image_scenes": 0,
    "public_research_enabled": False,
    "public_media_enabled": False,
    "style": "clean realistic documentary",
    "purpose": "acceptance smoke test",
}, ensure_ascii=False))
PY
curl -fsS "${headers[@]}" \
  -X POST "$BASE_URL/api/v1/asc-ai/director/plan" \
  --data-binary @"$TMP_DIR/plan-request.json" \
  -o "$TMP_DIR/plan.json" || fail "Director plan request failed"
json_assert "$TMP_DIR/plan.json" 'data["status"] == 200'
json_assert "$TMP_DIR/plan.json" 'data["data"]["local_only"] is True'
json_assert "$TMP_DIR/plan.json" 'data["data"]["gpu_policy"] == "scheduler_managed"'
json_assert "$TMP_DIR/plan.json" 'len(data["data"]["scenes"]) >= 1'
json_assert "$TMP_DIR/plan.json" 'all(x["visual_strategy"] == "LOCAL_IMAGE" for x in data["data"]["scenes"])'
echo "Director planning: OK"

if [[ "$ACCEPT_RENDER" != "1" ]]; then
  echo "[5/6] Full render skipped (set MPT_ACCEPT_RENDER=1 to enable bounded render)."
  echo "[6/6] Acceptance passed: compose + dependencies + local Director."
  exit 0
fi

echo "[5/6] Queueing bounded full local-only render..."
python3 - "$TOPIC" >"$TMP_DIR/render-request.json" <<'PY'
import json, sys
print(json.dumps({
    "video_subject": sys.argv[1],
    "video_language": "ru-RU",
    "video_aspect": "9:16",
    "target_duration_seconds": 12,
    "max_local_video_scenes": 0,
    "max_public_image_scenes": 0,
    "public_research_enabled": False,
    "public_media_enabled": False,
    "subtitle_enabled": True,
    "bgm_type": "",
    "video_count": 1,
    "style": "clean realistic documentary",
    "purpose": "bounded end-to-end acceptance",
}, ensure_ascii=False))
PY
curl -fsS "${headers[@]}" \
  -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -X POST "$BASE_URL/api/v1/asc-ai/production" \
  --data-binary @"$TMP_DIR/render-request.json" \
  -o "$TMP_DIR/submit.json" || fail "production submit failed"
TASK_ID="$(json_value "$TMP_DIR/submit.json" "data.task_id")"
[[ -n "$TASK_ID" ]] || fail "production returned no task_id"
echo "Task: $TASK_ID"

deadline=$(( $(date +%s) + RENDER_TIMEOUT ))
while :; do
  curl -fsS "${headers[@]}" "$BASE_URL/api/v1/tasks/$TASK_ID" \
    -o "$TMP_DIR/task.json" || fail "task polling failed"
  state="$(json_value "$TMP_DIR/task.json" "data.state")"
  progress="$(json_value "$TMP_DIR/task.json" "data.progress")"
  echo "  state=$state progress=$progress%"
  if [[ "$state" == "1" ]]; then
    break
  fi
  if [[ "$state" == "-1" ]]; then
    cat "$TMP_DIR/task.json" >&2
    fail "production task failed"
  fi
  if (( $(date +%s) >= deadline )); then
    fail "production timed out after ${RENDER_TIMEOUT}s"
  fi
  sleep "$POLL_SECONDS"
done

echo "[6/6] Validating production evidence..."
curl -fsS "${headers[@]}" \
  "$BASE_URL/api/v1/asc-ai/production/$TASK_ID/evidence" \
  -o "$TMP_DIR/evidence.json" || fail "evidence endpoint failed"
json_assert "$TMP_DIR/evidence.json" 'data["status"] == 200'
json_assert "$TMP_DIR/evidence.json" 'data["data"]["artifacts"]["director_plan"] is not None'
json_assert "$TMP_DIR/evidence.json" 'data["data"]["artifacts"]["director_execution_plan"] is not None'
json_assert "$TMP_DIR/evidence.json" 'data["data"]["artifacts"]["production_manifest"]["status"] == "complete"'
json_assert "$TMP_DIR/evidence.json" 'len(data["data"]["artifacts"]["production_manifest"]["outputs"]) >= 1'
echo "FULL ASC-AI ACCEPTANCE PASSED: $TASK_ID"
