#!/usr/bin/env bash
# Train the doc_quality_kr skill using the Claude CLI (no API key required).
#
# Prerequisites:
#   1. `claude` CLI is installed and logged in (`claude /login` once).
#   2. data/doc_quality_kr_split/{train,val,test}/items.json exist
#      (run scripts/ingest_confluence.py first).
#
# Usage:
#   bash scripts/run_doc_quality_kr.sh
#   bash scripts/run_doc_quality_kr.sh --num_epochs 2 --batch_size 4
#   bash scripts/run_doc_quality_kr.sh --split_dir /path/to/my_split

set -euo pipefail
cd "$(dirname "$0")/.."

exec python scripts/train.py \
    --config configs/doc_quality_kr/default.yaml \
    --backend claude_chat \
    "$@"
