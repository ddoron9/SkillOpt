"""LLM-as-judge evaluator for the Korean documentation quality benchmark.

The judge uses the same chat backend as the optimizer (typically `claude_chat`,
i.e. the Claude CLI). It scores a generated document along six dimensions and
returns a soft score in [0, 1] plus a binary `hard` (pass/fail) flag.

Score dimensions
----------------
- natural_korean : 영어 단어 섞임이나 어색한 번역체 없이 자연스러운 한국어인지
- readability    : 목차/구조/단락 분할이 청자의 읽는 순서를 잘 안내하는지
- brevity        : 같은 말 반복 없이 분량이 적절한지
- word_choice    : 잘 쓰지 않는 한자어/외래어를 피하고 한국 직장에서 통용되는 단어를 쓰는지
- fidelity       : 주어진 컨텍스트(소스 자료)에 충실하고 사실관계가 맞는지
- style_match    : 레퍼런스 문서의 톤·구조와 일관되는지

Each dimension is scored 0–5. The soft score is the mean / 5. Pass threshold
defaults to 0.7.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from skillopt.model.claude_backend import chat_with_deployment


_JUDGE_SYSTEM = """\
당신은 한국어 기술 문서 품질을 채점하는 평가자입니다.

평가 대상:
- INSTRUCTION: 문서 작성 지시문 (청자, 목적, 제약 등)
- CONTEXT: 작성자가 참고할 소스 자료
- REFERENCE: 사내에서 통용되는 좋은 문서 예시 (목표 스타일)
- GENERATED: 평가할 문서

채점 항목 (각 0~5점, 정수):
- natural_korean: 영어 단어 섞임이나 어색한 번역체 없이 자연스러운 한국어인지 (5점=완벽한 자연스러움, 0점=한국인이 쓰지 않을 문장)
- readability: 목차/구조/단락 분할이 청자의 읽는 순서를 잘 안내하는지 (5점=결론·핵심이 앞에 있고 순서가 직관적, 0점=뒤죽박죽)
- brevity: 같은 말 반복 없이 분량이 적절한지 (5점=한 번에 읽고 행동 가능, 0점=장황하거나 너무 짧음)
- word_choice: 잘 쓰지 않는 한자어/번역어/외래어를 피하고 사내에서 통용되는 단어를 쓰는지
- fidelity: CONTEXT의 사실관계에 충실한지 (환각 없음)
- style_match: REFERENCE의 톤/구조와 얼마나 일치하는지

다음 JSON 스키마로만 응답합니다. 코드 펜스나 설명 없이 JSON 한 덩어리만:
{
  "natural_korean": <int 0-5>,
  "readability": <int 0-5>,
  "brevity": <int 0-5>,
  "word_choice": <int 0-5>,
  "fidelity": <int 0-5>,
  "style_match": <int 0-5>,
  "rationale": "<3~5문장으로 핵심 감점 사유를 한국어로 요약>",
  "key_issues": ["<짧은 한 줄로 개별 이슈>", ...]
}
"""


_DIMENSIONS = (
    "natural_korean",
    "readability",
    "brevity",
    "word_choice",
    "fidelity",
    "style_match",
)


def _truncate(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n...[중략]"


def _build_judge_user(
    *,
    instruction: str,
    context: str,
    reference: str,
    generated: str,
) -> str:
    return (
        f"## INSTRUCTION\n{_truncate(instruction, 2000)}\n\n"
        f"## CONTEXT\n{_truncate(context, 4000)}\n\n"
        f"## REFERENCE (목표 스타일)\n{_truncate(reference, 4000)}\n\n"
        f"## GENERATED (평가 대상)\n{_truncate(generated, 6000)}"
    )


def _parse_judge_payload(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("judge returned empty response")
    # strip optional code fences just in case
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```\s*$", "", raw).strip()
    # find the outermost JSON object
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"judge response is not JSON: {raw[:200]}")
    return json.loads(raw[start : end + 1])


def evaluate(
    *,
    instruction: str,
    context: str,
    reference: str,
    generated: str,
    judge_model: str,
    pass_threshold: float = 0.7,
    timeout: int = 120,
) -> dict[str, Any]:
    """Score one generation. Returns a dict consumable by the trainer.

    Keys: ``soft`` (float in [0,1]), ``hard`` (0/1), ``dimensions`` (dict),
    ``rationale`` (str), ``key_issues`` (list[str]), ``judge_raw`` (str).
    """
    user = _build_judge_user(
        instruction=instruction,
        context=context,
        reference=reference,
        generated=generated,
    )
    raw, _ = chat_with_deployment(
        deployment=judge_model,
        system=_JUDGE_SYSTEM,
        user=user,
        max_completion_tokens=2048,
        retries=3,
        stage="judge",
        timeout=timeout,
    )

    result: dict[str, Any] = {
        "soft": 0.0,
        "hard": 0,
        "dimensions": {dim: 0 for dim in _DIMENSIONS},
        "rationale": "",
        "key_issues": [],
        "judge_raw": raw,
        "judge_ok": False,
    }
    try:
        payload = _parse_judge_payload(raw)
    except Exception as exc:  # noqa: BLE001
        result["rationale"] = f"judge parse error: {exc}"
        return result

    scores: list[int] = []
    for dim in _DIMENSIONS:
        value = payload.get(dim)
        try:
            int_value = max(0, min(5, int(value)))
        except (TypeError, ValueError):
            int_value = 0
        result["dimensions"][dim] = int_value
        scores.append(int_value)

    soft = (sum(scores) / (5 * len(scores))) if scores else 0.0
    result["soft"] = soft
    result["hard"] = int(soft >= pass_threshold)
    result["rationale"] = str(payload.get("rationale", "") or "")
    issues = payload.get("key_issues") or []
    if isinstance(issues, list):
        result["key_issues"] = [str(item) for item in issues if item]
    result["judge_ok"] = True
    return result
