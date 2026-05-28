"""Dataloader for the code quality benchmark."""
from __future__ import annotations

from skillopt.datasets.base import SplitDataLoader, _load_json_or_jsonl


class CodeQualityKRDataLoader(SplitDataLoader):
    def load_raw_items(self, data_path: str) -> list[dict]:
        return _load_json_or_jsonl(data_path)
