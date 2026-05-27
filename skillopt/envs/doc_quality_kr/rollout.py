"""Rollout for the Korean documentation quality benchmark.

For each item: build a system prompt that injects the current skill, ask the
target model to draft the documentation, then run the LLM-as-judge evaluator
and record both the draft and the rubric scores.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from skillopt.envs.doc_quality_kr.evaluator import evaluate as _judge
from skillopt.model import chat_target
from skillopt.prompts import load_prompt


_MAX_CONTEXT_CHARS = 8000
_MAX_REFERENCE_CHARS = 6000


def _truncate(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n...[중략]"


def _build_system(skill_content: str) -> str:
    if skill_content.strip():
        skill_section = f"## Skill\n{skill_content.strip()}\n\n"
    else:
        skill_section = ""
    return load_prompt("rollout_system", env="doc_quality_kr").format(skill_section=skill_section)


def _build_user(item: dict) -> str:
    instruction = (item.get("instruction") or "").strip()
    context = _truncate(item.get("context", ""), _MAX_CONTEXT_CHARS)
    audience = (item.get("audience") or "").strip()
    doc_type = (item.get("doc_type") or "").strip()
    style_hints = (item.get("style_hints") or "").strip()

    parts = [f"## INSTRUCTION\n{instruction}"]
    if audience:
        parts.append(f"## 대상 독자\n{audience}")
    if doc_type:
        parts.append(f"## 문서 유형\n{doc_type}")
    if style_hints:
        parts.append(f"## 추가 스타일 힌트\n{style_hints}")
    if context:
        parts.append(f"## 참고 컨텍스트\n{context}")
    parts.append(
        "## 출력 형식\n"
        "문서 본문만 `<doc>` 태그 안에 작성합니다. 머리말이나 설명을 본문 밖에 두지 않습니다.\n\n"
        "<doc>\n...\n</doc>"
    )
    return "\n\n".join(parts)


def _extract_doc(response: str) -> str:
    if not response:
        return ""
    lowered = response.lower()
    start = lowered.find("<doc>")
    end = lowered.rfind("</doc>")
    if start != -1 and end != -1 and end > start:
        return response[start + len("<doc>") : end].strip()
    return response.strip()


def process_one(
    item: dict,
    out_root: str,
    skill_content: str,
    exec_timeout: int,
    judge_model: str,
    pass_threshold: float,
) -> dict:
    item_id = str(item["id"])
    instruction = item.get("instruction", "")
    result = {
        "id": item_id,
        "task_type": item.get("task_type") or "doc",
        "task_description": instruction,
        "instruction": instruction,
        "hard": 0,
        "soft": 0.0,
        "generated_doc": "",
        "response": "",
        "fail_reason": "",
        "agent_ok": False,
        "judge_ok": False,
        "judge_dimensions": {},
        "judge_rationale": "",
        "judge_key_issues": [],
    }

    pred_dir = os.path.join(out_root, "predictions", item_id)
    os.makedirs(pred_dir, exist_ok=True)

    try:
        system = _build_system(skill_content)
        user = _build_user(item)
        response, _ = chat_target(
            system=system,
            user=user,
            max_completion_tokens=4096,
            retries=3,
            stage="rollout",
            timeout=exec_timeout,
        )
        result["response"] = response
        result["agent_ok"] = True
        doc = _extract_doc(response)
        result["generated_doc"] = doc

        with open(os.path.join(pred_dir, "target_system_prompt.txt"), "w", encoding="utf-8") as f:
            f.write(system)
        with open(os.path.join(pred_dir, "target_user_prompt.txt"), "w", encoding="utf-8") as f:
            f.write(user)
        with open(os.path.join(pred_dir, "generated_doc.md"), "w", encoding="utf-8") as f:
            f.write(doc)

        reference = _truncate(item.get("reference_doc", ""), _MAX_REFERENCE_CHARS)
        judge = _judge(
            instruction=item.get("instruction", ""),
            context=item.get("context", ""),
            reference=reference,
            generated=doc,
            judge_model=judge_model,
            pass_threshold=pass_threshold,
            timeout=exec_timeout,
        )
        result["hard"] = judge["hard"]
        result["soft"] = judge["soft"]
        result["judge_ok"] = judge["judge_ok"]
        result["judge_dimensions"] = judge["dimensions"]
        result["judge_rationale"] = judge["rationale"]
        result["judge_key_issues"] = judge["key_issues"]
        if judge["soft"] < pass_threshold:
            result["fail_reason"] = judge["rationale"] or "soft score below threshold"

        # Persist a conversation file so the reflection step can read it.
        conversation = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": response},
            {
                "role": "system",
                "content": (
                    "[EVALUATION RESULT]\n"
                    f"soft={judge['soft']:.3f} hard={judge['hard']} "
                    f"threshold={pass_threshold}\n"
                    f"dimensions={json.dumps(judge['dimensions'], ensure_ascii=False)}\n"
                    f"rationale={judge['rationale']}\n"
                    f"key_issues={json.dumps(judge['key_issues'], ensure_ascii=False)}"
                ),
            },
        ]
        with open(os.path.join(pred_dir, "conversation.json"), "w", encoding="utf-8") as f:
            json.dump(conversation, f, ensure_ascii=False, indent=2)
        with open(os.path.join(pred_dir, "judge.json"), "w", encoding="utf-8") as f:
            json.dump(judge, f, ensure_ascii=False, indent=2)

    except Exception as exc:  # noqa: BLE001
        result["fail_reason"] = f"error: {type(exc).__name__}: {exc}"
        with open(os.path.join(pred_dir, "error.txt"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())

    return result


def run_batch(
    items: list[dict],
    out_root: str,
    skill_content: str,
    exec_timeout: int,
    workers: int,
    judge_model: str,
    pass_threshold: float,
    task_timeout: int = 600,
) -> list[dict]:
    """Resume-aware parallel rollout."""
    task_timeout = max(int(task_timeout), int(exec_timeout) + 60)
    results_path = os.path.join(out_root, "results.jsonl")
    os.makedirs(out_root, exist_ok=True)

    done_ids: set[str] = set()
    existing: list[dict] = []
    if os.path.exists(results_path):
        with open(results_path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    done_ids.add(str(row["id"]))
                    existing.append(row)
                except Exception:
                    continue

    pending = [it for it in items if str(it["id"]) not in done_ids]
    if not pending:
        return existing

    total = len(existing) + len(pending)
    completed = len(existing)
    pass_count = sum(1 for r in existing if r.get("hard", 0))
    if existing:
        print(f"    [rollout] resuming: {completed}/{total} already done", flush=True)

    results = list(existing)
    started_at: dict[str, float] = {}

    def _timeout_row(item: dict) -> dict:
        instruction = item.get("instruction", "")
        return {
            "id": str(item["id"]),
            "task_type": item.get("task_type") or "doc",
            "task_description": instruction,
            "instruction": instruction,
            "hard": 0,
            "soft": 0.0,
            "generated_doc": "",
            "response": "",
            "fail_reason": f"task-timeout-{task_timeout}s",
            "agent_ok": False,
            "judge_ok": False,
            "judge_dimensions": {},
            "judge_rationale": "",
            "judge_key_issues": [],
        }

    def _run_one(item: dict) -> dict:
        started_at[str(item["id"])] = time.time()
        return process_one(
            item=item,
            out_root=out_root,
            skill_content=skill_content,
            exec_timeout=exec_timeout,
            judge_model=judge_model,
            pass_threshold=pass_threshold,
        )

    with open(results_path, "a", encoding="utf-8") as outf:
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            futs = {ex.submit(_run_one, it): it for it in pending}
            pending_futs = set(futs)
            while pending_futs:
                done, _ = wait(pending_futs, timeout=5, return_when=FIRST_COMPLETED)
                now = time.time()
                timed_out = [
                    fut for fut in pending_futs - done
                    if str(futs[fut]["id"]) in started_at
                    and now - started_at[str(futs[fut]["id"])] >= task_timeout
                ]
                for fut in done:
                    pending_futs.remove(fut)
                    item = futs[fut]
                    try:
                        row = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        row = _timeout_row(item)
                        row["fail_reason"] = f"unexpected: {type(exc).__name__}: {exc}"
                    results.append(row)
                    completed += 1
                    if row.get("hard", 0):
                        pass_count += 1
                    pass_rate = pass_count / completed if completed else 0
                    print(
                        f"    [rollout] {completed}/{total} "
                        f"(pass={pass_rate:.3f} soft={row.get('soft', 0):.3f}) "
                        f"id={row['id']}",
                        flush=True,
                    )
                    outf.write(json.dumps(row, ensure_ascii=False) + "\n")
                    outf.flush()
                for fut in timed_out:
                    pending_futs.remove(fut)
                    fut.cancel()
                    row = _timeout_row(futs[fut])
                    results.append(row)
                    completed += 1
                    print(
                        f"    [rollout] {completed}/{total} TIMEOUT id={row['id']}",
                        flush=True,
                    )
                    outf.write(json.dumps(row, ensure_ascii=False) + "\n")
                    outf.flush()
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    return results
