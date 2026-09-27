#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${CHATTERBOX_URL:-http://127.0.0.1:4123}"
VOICE_NAME="${CHATTERBOX_RU_VOICE_NAME:-ru-default}"

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/russian-reference.wav" >&2
  exit 2
fi

VOICE_FILE="$1"
if [[ ! -f "$VOICE_FILE" ]]; then
  echo "Voice sample not found: $VOICE_FILE" >&2
  exit 2
fi

echo "Checking Chatterbox health at $BASE_URL ..."
curl -fsS "$BASE_URL/health" >/dev/null

if curl -fsS "$BASE_URL/v1/voices" | CHATTERBOX_RU_VOICE_NAME="$VOICE_NAME" python3 -c '
import json, os, sys
name=os.environ.get("CHATTERBOX_RU_VOICE_NAME","ru-default")
data=json.load(sys.stdin)
for row in data.get("voices") or []:
    if row.get("name")==name or name in (row.get("aliases") or []):
        if str(row.get("language") or "").lower()!="ru":
            raise SystemExit(3)
        raise SystemExit(0)
raise SystemExit(1)
'; then
  echo "Voice '$VOICE_NAME' already exists with language=ru."
  exit 0
else
  status=$?
  if [[ $status -eq 3 ]]; then
    echo "Voice '$VOICE_NAME' exists but is not marked language=ru. Delete/fix it first." >&2
    exit 3
  fi
fi

echo "Uploading '$VOICE_NAME' as Russian voice..."
curl -fsS -X POST "$BASE_URL/v1/voices"   -F "voice_name=$VOICE_NAME"   -F "language=ru"   -F "voice_file=@$VOICE_FILE" >/dev/null

echo "Verifying voice metadata..."
CHATTERBOX_RU_VOICE_NAME="$VOICE_NAME" curl -fsS "$BASE_URL/v1/voices" |   CHATTERBOX_RU_VOICE_NAME="$VOICE_NAME" python3 -c '
import json, os, sys
name=os.environ["CHATTERBOX_RU_VOICE_NAME"]
data=json.load(sys.stdin)
row=next((r for r in data.get("voices") or [] if r.get("name")==name), None)
if not row:
    raise SystemExit("uploaded voice is missing from Chatterbox catalog")
if str(row.get("language") or "").lower()!="ru":
    raise SystemExit("uploaded voice language is not ru")
print(f"OK: {name} language=ru")
'
