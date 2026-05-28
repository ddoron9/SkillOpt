"""Dataloader for the Korean documentation quality benchmark.

Each split directory (`train/`, `val/`, `test/`) contains a single JSON file
with an array of task items. See `data/doc_quality_kr_seed/README.md` for
the expected schema.
"""
from __future__ import annotations

from skillopt.datasets.base import SplitDataLoader, _load_json_or_jsonl


class DocQualityKRDataLoader(SplitDataLoader):
    def load_raw_items(self, data_path: str) -> list[dict]:
        return _load_json_or_jsonl(data_path)
