"""LLM-as-judge evaluator for the code quality benchmark.

The judge compares a generated diff against a reference diff and the
original task instruction, scoring six code-quality dimensions in [0, 5]:

- correctness        : 요구사항을 충족하고 동작이 깨지지 않는가
- minimality         : 요구한 만큼만 건드렸는가 (불필요한 리팩토링 없음)
- no_over_engineering: 미래 가정·과한 추상화·헛 옵션이 없는가
- readability        : 변수/함수 이름과 구조가 읽기 쉬운가
- style_match        : 레퍼런스 diff의 스타일을 따르는가
- no_redundancy      : 중복 코드·중복 책임이 없는가

Soft score = mean / 5. Pass threshold defaults to 0.7.
"""
from __future__ import annotations

import json
import re
from typing import Any

from skillopt.model.claude_backend import chat_with_deployment


_JUDGE_SYSTEM = """\
당신은 코드 변경의 품질을 채점하는 평가자입니다.

평가 대상:
- INSTRUCTION: 작업 지시문 (어떤 변경을 요구했는지)
- CONTEXT_FILES: 변경 전 파일 일부 (참고용)
- REFERENCE_DIFF: 사내에서 실제로 머지된 좋은 변경 (목표 스타일)
- GENERATED_DIFF: 평가할 변경

채점 항목 (각 0~5점, 정수):
- correctness: 지시문을 충족하고, 동작이 깨지지 않는가. 빠뜨린 변경·잘못 건드린 변경이 없는가.
- minimality: 요구한 범위만 건드렸는가. 부수적 리팩토링이나 무관 변경이 없는가.
- no_over_engineering: 미래에 필요할지 모를 옵션·추상화·플래그가 없는가. 호출자에게 필요 없는 매개변수를 추가하지 않았는가.
- readability: 변수/함수 이름이 의미를 잘 드러내고, 구조가 추적 가능한가. `data`, `info`, `result` 같은 일반 이름을 피하는가.
- style_match: REFERENCE_DIFF의 어조와 컨벤션(들여쓰기, 주석 빈도, 분기 깊이 등)을 따르는가.
- no_redundancy: 같은 일을 두 번 하지 않는가. 이미 있는 함수를 다시 만들지 않았는가.

다음 JSON 한 덩어리로만 응답합니다. 코드 펜스나 설명 없이:
{
  "correctness": <int 0-5>,
  "minimality": <int 0-5>,
  "no_over_engineering": <int 0-5>,
  "readability": <int 0-5>,
  "style_match": <int 0-5>,
  "no_redundancy": <int 0-5>,
  "rationale": "<3~5문장으로 핵심 감점 사유를 한국어로 요약>",
  "key_issues": ["<짧은 한 줄로 개별 이슈>", ...]
}
"""


_DIMENSIONS = (
    "correctness",
    "minimality",
    "no_over_engineering",
    "readability",
    "style_match",
    "no_redundancy",
)


def _truncate(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n...[중략]"


def _build_judge_user(
    *,
    instruction: str,
    context_files: str,
    reference: str,
    generated: str,
) -> str:
    return (
        f"## INSTRUCTION\n{_truncate(instruction, 2000)}\n\n"
        f"## CONTEXT_FILES\n{_truncate(context_files, 4000)}\n\n"
        f"## REFERENCE_DIFF (목표 스타일)\n{_truncate(reference, 6000)}\n\n"
        f"## GENERATED_DIFF (평가 대상)\n{_truncate(generated, 8000)}"
    )


def _parse_judge_payload(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("judge returned empty response")
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```\s*$", "", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"judge response is not JSON: {raw[:200]}")
    return json.loads(raw[start : end + 1])


def evaluate(
    *,
    instruction: str,
    context_files: str,
    reference: str,
    generated: str,
    judge_model: str,
    pass_threshold: float = 0.7,
    timeout: int = 120,
) -> dict[str, Any]:
    """Score one diff. Returns dict consumable by the trainer."""
    user = _build_judge_user(
        instruction=instruction,
        context_files=context_files,
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
