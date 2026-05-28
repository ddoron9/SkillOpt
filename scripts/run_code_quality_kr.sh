#!/usr/bin/env bash
# Train the code_quality_kr skill using the Claude Code CLI as target
# and the Claude chat CLI as optimizer + judge. No API key required.
#
# Prerequisites:
#   1. `claude` CLI installed and logged in (`claude /login` once).
#   2. data/code_quality_kr_split/{train,val,test}/items.json exist
#      (run scripts/ingest_bitbucket.py first).
#
# Usage:
#   bash scripts/run_code_quality_kr.sh
#   bash scripts/run_code_quality_kr.sh --num_epochs 1 --batch_size 4 --workers 1

set -euo pipefail
cd "$(dirname "$0")/.."

exec python scripts/train.py \
    --config configs/code_quality_kr/default.yaml \
    --backend claude_code_exec \
    "$@"
