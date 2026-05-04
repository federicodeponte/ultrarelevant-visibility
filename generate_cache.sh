#!/bin/bash
# Generate cached visibility scores for popular industrial companies
# Each company takes ~90s; we run 5 in parallel via the local engine's job queue.

ENGINE="http://127.0.0.1:18766"
CACHE_DIR="/root/visibility-deploy/cache"
mkdir -p "$CACHE_DIR"

COMPANIES=("igus" "Festo" "Bosch Rexroth" "SKF" "Siemens")

declare -A JOBS

# Submit all jobs
for company in "${COMPANIES[@]}"; do
  slug=$(echo "$company" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')
  if [[ -f "$CACHE_DIR/$slug.json" ]] && [[ "$slug" == "igus" ]]; then
    echo "[skip] $slug (already cached)"
    continue
  fi
  resp=$(curl -s -X POST "$ENGINE/analyze" -H "Content-Type: application/json" -d "{\"company_name\": \"$company\"}")
  job_id=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin).get('job_id',''))")
  echo "[submit] $company => job=$job_id"
  JOBS[$slug]="$job_id"
done

# Poll until all done
echo ""
echo "Waiting for jobs (max 300s each)..."
DONE=0
START=$(date +%s)
while [[ $DONE -lt ${#JOBS[@]} ]]; do
  DONE=0
  for slug in "${!JOBS[@]}"; do
    job_id="${JOBS[$slug]}"
    if [[ -f "$CACHE_DIR/$slug.json" ]] && [[ -s "$CACHE_DIR/$slug.json" ]]; then
      DONE=$((DONE+1))
      continue
    fi
    status=$(curl -s "$ENGINE/status/$job_id")
    state=$(echo "$status" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    if [[ "$state" == "complete" ]]; then
      echo "$status" | python3 -c "import json,sys; d=json.load(sys.stdin); json.dump(d.get('result',{}), open('$CACHE_DIR/$slug.json','w'))"
      echo "[done] $slug score=$(python3 -c "import json; print(json.load(open('$CACHE_DIR/$slug.json')).get('score'))")"
      DONE=$((DONE+1))
    elif [[ "$state" == "error" ]]; then
      err=$(echo "$status" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error',''))")
      echo "[ERROR] $slug: $err"
      DONE=$((DONE+1))
    fi
  done
  if [[ $DONE -lt ${#JOBS[@]} ]]; then
    elapsed=$(($(date +%s) - START))
    echo "  ... $DONE/${#JOBS[@]} done (${elapsed}s elapsed)"
    sleep 5
  fi
  if [[ $(($(date +%s) - START)) -gt 600 ]]; then
    echo "TIMEOUT after 600s"
    break
  fi
done

echo ""
echo "=== final cache ==="
ls -la "$CACHE_DIR"
