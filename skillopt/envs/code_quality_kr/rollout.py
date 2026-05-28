"""Rollout for the code quality benchmark.

Each item is a task to produce a code change that should look like the
reference diff. We spin up a per-item workspace with the pre-change files,
run Claude Code CLI (`claude_code_exec`) against an injected skill, and
compute the resulting diff via :func:`difflib.unified_diff`. The diff is
then handed to the LLM judge in :mod:`evaluator`.
"""
from __future__ import annotations

import difflib
import json
import os
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from skillopt.envs.code_quality_kr.evaluator import evaluate as _judge
from skillopt.model import azure_openai as _llm
from skillopt.model.codex_harness import (
    prepare_workspace,
    render_skill_md,
    run_target_exec,
)
from skillopt.prompts import load_prompt


_MAX_REFERENCE_CHARS = 6000
_MAX_CONTEXT_PER_FILE = 4000
_MAX_GENERATED_DIFF_CHARS = 10000


def _truncate(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n...[중략]"


def _build_task_text(item: dict) -> str:
    instruction = (item.get("instruction") or "").strip()
    style_hints = (item.get("style_hints") or "").strip()
    parts = [f"## 작업 지시\n{instruction}"]
    if style_hints:
        parts.append(f"## 스타일 힌트\n{style_hints}")
    if item.get("context_files"):
        names = ", ".join(f["path"] for f in item["context_files"])
        parts.append(f"## 작업할 파일\n{names}")
    parts.append(
        "## 출력\n"
        "변경할 파일을 직접 수정해 저장합니다. 추가 설명이나 요약은 출력하지 않습니다."
    )
    return "\n\n".join(parts)


def _context_files_to_extra(item: dict) -> dict[str, str]:
    extra: dict[str, str] = {}
    for entry in item.get("context_files") or []:
        path = entry.get("path")
        content = entry.get("content", "")
        if path:
            extra[path] = content
    return extra


def _render_context_for_judge(item: dict) -> str:
    lines: list[str] = []
    for entry in item.get("context_files") or []:
        path = entry.get("path", "")
        content = _truncate(entry.get("content", ""), _MAX_CONTEXT_PER_FILE)
        lines.append(f"### {path}\n```\n{content}\n```")
    return "\n\n".join(lines)


def _compute_generated_diff(
    work_dir: str,
    originals: dict[str, str],
) -> str:
    chunks: list[str] = []
    seen: set[str] = set()
    for rel_path, original in originals.items():
        full_path = os.path.join(work_dir, rel_path)
        modified = ""
        if os.path.isfile(full_path):
            with open(full_path, encoding="utf-8", errors="replace") as f:
                modified = f.read()
        seen.add(rel_path)
        if modified == original:
            continue
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=3,
        )
        chunks.append("".join(diff))

    for root, _dirs, files in os.walk(work_dir):
        for name in files:
            if name == "task.md":
                continue
            rel = os.path.relpath(os.path.join(root, name), work_dir)
            if rel.startswith(".agents/") or rel in seen:
                continue
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                added = f.read()
            diff = difflib.unified_diff(
                [],
                added.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{rel}",
                n=3,
            )
            chunks.append("".join(diff))

    return "".join(chunks)


def _build_codex_skill(skill_content: str) -> str:
    return render_skill_md(
        skill_content,
        description="Dynamic skill for the current code-change task.",
        preamble=(
            "이 스킬은 현재 디렉토리의 파일을 직접 수정해 작업을 완료할 때 사용합니다.\n"
            "task.md를 읽고, 변경할 파일을 그대로 편집한 뒤 저장합니다.\n"
            "결과 요약이나 설명은 출력하지 않습니다."
        ),
    )


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
        "task_type": item.get("task_type") or "code",
        "task_description": instruction,
        "instruction": instruction,
        "hard": 0,
        "soft": 0.0,
        "generated_diff": "",
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
        originals = _context_files_to_extra(item)
        task_text = _build_task_text(item)
        skill_md = _build_codex_skill(skill_content)

        work_dir = os.path.join(pred_dir, "workspace")
        prepare_workspace(
            work_dir=work_dir,
            skill_md=skill_md,
            task_text=task_text,
            extra_files=originals,
        )

        prompt = (
            "`skillopt-target` 스킬을 사용해 task.md의 요구사항을 처리합니다.\n"
            "변경 대상 파일을 직접 편집해 저장한 뒤, 추가 설명 없이 종료합니다."
        )
        final_message, raw_response = run_target_exec(
            work_dir=work_dir,
            prompt=prompt,
            model=_llm.TARGET_DEPLOYMENT,
            timeout=exec_timeout,
        )
        result["response"] = final_message or raw_response or ""
        result["agent_ok"] = True

        generated_diff = _compute_generated_diff(work_dir, originals)
        generated_diff = _truncate(generated_diff, _MAX_GENERATED_DIFF_CHARS)
        result["generated_diff"] = generated_diff

        with open(os.path.join(pred_dir, "target_task.md"), "w", encoding="utf-8") as f:
            f.write(task_text)
        with open(os.path.join(pred_dir, "generated_diff.patch"), "w", encoding="utf-8") as f:
            f.write(generated_diff)

        reference_diff = _truncate(item.get("reference_diff", ""), _MAX_REFERENCE_CHARS)
        context_blob = _render_context_for_judge(item)
        judge = _judge(
            instruction=item.get("instruction", ""),
            context_files=context_blob,
            reference=reference_diff,
            generated=generated_diff,
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

        conversation = [
            {"role": "system", "content": skill_md},
            {"role": "user", "content": task_text},
            {"role": "assistant", "content": result["response"]},
            {
                "role": "system",
                "content": (
                    "[EVALUATION RESULT]\n"
                    f"soft={judge['soft']:.3f} hard={judge['hard']} "
                    f"threshold={pass_threshold}\n"
                    f"dimensions={json.dumps(judge['dimensions'], ensure_ascii=False)}\n"
                    f"rationale={judge['rationale']}\n"
                    f"key_issues={json.dumps(judge['key_issues'], ensure_ascii=False)}\n"
                    f"generated_diff=\n{generated_diff[:2000]}"
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
    task_timeout: int = 1800,
) -> list[dict]:
    """Resume-aware parallel rollout for code-change tasks."""
    task_timeout = max(int(task_timeout), int(exec_timeout) + 120)
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
            "task_type": item.get("task_type") or "code",
            "task_description": instruction,
            "instruction": instruction,
            "hard": 0,
            "soft": 0.0,
            "generated_diff": "",
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
