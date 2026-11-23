#!/usr/bin/env bash
# Demo flow: upload two volumes, query change intervals, swap vol1, query again.
# Usage: scripts/demo_flow.sh [base_url]   (default http://127.0.0.1:8000)
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
URL="http://gazetteer.example.gov.cn/zcgg/notice.html"
DEMO_DIR="$(cd "$(dirname "$0")/.." && pwd)/demo"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

step "1. upload vol1.warc (t1 V1, t2 V2)"
curl -sS -X PUT "$BASE/volumes/vol1/warc" -F "file=@$DEMO_DIR/vol1.warc;type=application/warc" | jq .

step "2. upload vol2.warc (t3 revisit, t4 V3, t5 corrupt digest, t6 revisit)"
curl -sS -X PUT "$BASE/volumes/vol2/warc" -F "file=@$DEMO_DIR/vol2.warc;type=application/warc" | jq .

step "3. BEFORE swap: between which captures did the bulletin body change?"
curl -sS -G "$BASE/changes" --data-urlencode "url=$URL" \
  | jq '{changes, unevidenced}'

step "4. replace vol1 with vol1b.warc (V2 body dropped)"
curl -sS -X PUT "$BASE/volumes/vol1/warc" -F "file=@$DEMO_DIR/vol1b.warc;type=application/warc" | jq .

step "5. AFTER swap: t3 revisit lost evidence, intervals recomputed"
curl -sS -G "$BASE/changes" --data-urlencode "url=$URL" \
  | jq '{changes, unevidenced}'

step "6. conflicting upload is rejected (409) and old data kept"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X PUT "$BASE/volumes/vol3/warc" \
  -F "file=@$DEMO_DIR/conflict.warc;type=application/warc"
curl -sS "$BASE/volumes" | jq '[.volumes[] | {volume, records}]'
