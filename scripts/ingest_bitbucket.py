#!/usr/bin/env python3
"""Ingest Bitbucket Cloud pull requests into a code_quality_kr split.

Each PR becomes one training item:
- ``instruction``     : the PR title + description (작업이 무엇을 요구했는가)
- ``context_files``   : pre-change content of files touched by the PR
- ``reference_diff``  : the merged unified diff
- ``reference_meta``  : workspace / repo / pr id / url

Auth (cloud)
------------
- BITBUCKET_URL          (default: https://api.bitbucket.org)
- BITBUCKET_USER         (your Bitbucket username, *not* email)
- BITBUCKET_APP_PASSWORD (app password — Bitbucket → Personal settings →
                           App passwords; scopes: Repositories read +
                           Pull requests read)

Usage
-----
Mode A — discover PRs you authored across a workspace::

    python scripts/ingest_bitbucket.py \\
        --search-current-user-prs \\
        --workspace yourworkspace \\
        --repos repo-a,repo-b \\
        --max-prs 20 \\
        --redact \\
        --out_dir data/code_quality_kr_split

Mode B — explicit list of PRs::

    cat prs.json
    # [{"workspace": "wks", "repo": "repo-a", "pr_id": 42}, ...]
    python scripts/ingest_bitbucket.py \\
        --input prs.json \\
        --redact \\
        --out_dir data/code_quality_kr_split
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
from typing import Any, Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import redact as redact_mod  # noqa: E402


_BITBUCKET_DEFAULT_URL = "https://api.bitbucket.org"
_DIFF_FILE_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_MAX_FILES_PER_PR = 8
_MAX_FILE_CHARS = 8000


def _auth_header(user: str, app_password: str) -> str:
    token = base64.b64encode(f"{user}:{app_password}".encode()).decode()
    return f"Basic {token}"


def _http_get_json(url: str, headers: dict[str, str]) -> dict:
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=60) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    return json.loads(data) if data else {}


def _http_get_text(url: str, headers: dict[str, str]) -> str:
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _list_pull_requests(
    *,
    base_url: str,
    headers: dict[str, str],
    workspace: str,
    repo: str,
    author_uuid: str | None,
    state: str,
    page_size: int,
) -> Iterable[dict]:
    url = (
        f"{base_url}/2.0/repositories/{quote(workspace)}/{quote(repo)}"
        f"/pullrequests?state={state}&pagelen={page_size}"
    )
    if author_uuid:
        url += f"&q=author.uuid=%22{quote(author_uuid)}%22"
    while url:
        payload = _http_get_json(url, headers)
        for entry in payload.get("values", []) or []:
            yield entry
        url = payload.get("next") or ""


def _get_current_user_uuid(base_url: str, headers: dict[str, str]) -> str:
    payload = _http_get_json(f"{base_url}/2.0/user", headers)
    uuid = str(payload.get("uuid") or "")
    if not uuid:
        raise RuntimeError("Bitbucket /2.0/user did not return a uuid; check auth.")
    return uuid


def _get_pr_metadata(
    *,
    base_url: str,
    headers: dict[str, str],
    workspace: str,
    repo: str,
    pr_id: int | str,
) -> dict:
    url = (
        f"{base_url}/2.0/repositories/{quote(workspace)}/{quote(repo)}"
        f"/pullrequests/{pr_id}"
    )
    return _http_get_json(url, headers)


def _get_pr_diff(
    *,
    base_url: str,
    headers: dict[str, str],
    workspace: str,
    repo: str,
    pr_id: int | str,
) -> str:
    url = (
        f"{base_url}/2.0/repositories/{quote(workspace)}/{quote(repo)}"
        f"/pullrequests/{pr_id}/diff"
    )
    return _http_get_text(url, headers)


def _get_file_at_commit(
    *,
    base_url: str,
    headers: dict[str, str],
    workspace: str,
    repo: str,
    commit: str,
    path: str,
) -> str | None:
    url = (
        f"{base_url}/2.0/repositories/{quote(workspace)}/{quote(repo)}"
        f"/src/{quote(commit)}/{quote(path, safe='/')}"
    )
    try:
        return _http_get_text(url, headers)
    except Exception:  # noqa: BLE001
        return None


def _extract_paths_from_diff(diff: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for match in _DIFF_FILE_HEADER.finditer(diff):
        before, after = match.group(1), match.group(2)
        candidate = after if after != "/dev/null" else before
        if candidate and candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)
        if len(paths) >= _MAX_FILES_PER_PR:
            break
    return paths


def _derive_instruction(pr_meta: dict) -> str:
    title = (pr_meta.get("title") or "").strip()
    description = (pr_meta.get("description") or "").strip()
    if title and description:
        return f"{title}\n\n{description}"
    return title or description or "코드 변경 작업을 수행합니다."


def _build_item(
    *,
    base_url: str,
    headers: dict[str, str],
    entry: dict,
    redact_state: "redact_mod.RedactionState | None",
) -> dict:
    workspace = entry["workspace"]
    repo = entry["repo"]
    pr_id = entry["pr_id"]

    pr_meta = _get_pr_metadata(
        base_url=base_url, headers=headers,
        workspace=workspace, repo=repo, pr_id=pr_id,
    )
    diff_text = _get_pr_diff(
        base_url=base_url, headers=headers,
        workspace=workspace, repo=repo, pr_id=pr_id,
    )
    source_commit = (pr_meta.get("source") or {}).get("commit", {}).get("hash") or ""
    base_commit = (pr_meta.get("destination") or {}).get("commit", {}).get("hash") or ""

    paths = _extract_paths_from_diff(diff_text)
    context_files: list[dict] = []
    if base_commit:
        for path in paths:
            content = _get_file_at_commit(
                base_url=base_url, headers=headers,
                workspace=workspace, repo=repo,
                commit=base_commit, path=path,
            )
            if content is None:
                continue
            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS] + "\n...[중략]"
            context_files.append({"path": path, "content": content})

    instruction = _derive_instruction(pr_meta)

    meta = {
        "source": "bitbucket",
        "workspace": workspace,
        "repo": repo,
        "pr_id": pr_id,
        "title": pr_meta.get("title", ""),
        "url": (pr_meta.get("links") or {}).get("html", {}).get("href", ""),
        "source_commit": source_commit,
        "base_commit": base_commit,
        "merged": (pr_meta.get("state") == "MERGED"),
    }

    if redact_state is not None:
        diff_text = redact_mod.redact(diff_text, redact_state)
        instruction = redact_mod.redact(instruction, redact_state)
        for entry_ctx in context_files:
            entry_ctx["content"] = redact_mod.redact(entry_ctx["content"], redact_state)
            entry_ctx["path"] = redact_mod.redact(entry_ctx["path"], redact_state)
        meta = redact_mod.redact_meta(meta, redact_state)
        meta["title"] = redact_mod.redact(str(meta.get("title", "")), redact_state)
        meta["redacted"] = True

    return {
        "id": entry.get("custom_id") or f"bitbucket-{workspace}-{repo}-{pr_id}",
        "task_type": entry.get("task_type") or "code",
        "instruction": instruction,
        "style_hints": entry.get("style_hints", ""),
        "context_files": context_files,
        "reference_diff": diff_text,
        "reference_meta": meta,
    }


def _parse_split_ratio(text: str) -> tuple[int, int, int]:
    parts = [p.strip() for p in text.split(":") if p.strip()]
    if len(parts) != 3:
        raise ValueError(f"--split_ratio must be train:val:test, got {text!r}")
    train, val, test = (int(p) for p in parts)
    if min(train, val, test) < 0 or train + val + test == 0:
        raise ValueError(f"invalid ratio: {text!r}")
    return train, val, test


def _compute_split_counts(total: int, ratio: tuple[int, int, int]) -> tuple[int, int, int]:
    denom = sum(ratio)
    counts = [total * w // denom for w in ratio]
    remaining = total - sum(counts)
    order = sorted(
        range(3),
        key=lambda i: (total * ratio[i] / denom - counts[i], ratio[i]),
        reverse=True,
    )
    for i in order[:remaining]:
        counts[i] += 1
    return counts[0], counts[1], counts[2]


def _write_split(out_dir: str, items: list[dict], split_ratio: str, seed: int) -> None:
    ratio = _parse_split_ratio(split_ratio)
    shuffled = list(items)
    rng = random.Random(seed)
    rng.shuffle(shuffled)
    train_n, val_n, _ = _compute_split_counts(len(shuffled), ratio)
    splits = {
        "train": shuffled[:train_n],
        "val": shuffled[train_n : train_n + val_n],
        "test": shuffled[train_n + val_n :],
    }
    for name, rows in splits.items():
        split_path = os.path.join(out_dir, name)
        os.makedirs(split_path, exist_ok=True)
        with open(os.path.join(split_path, "items.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"  wrote {len(rows):3d} items → {split_path}/items.json")

    with open(os.path.join(out_dir, "split_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({
            "source": "bitbucket",
            "total": len(items),
            "split_ratio": split_ratio,
            "seed": seed,
            "counts": {name: len(rows) for name, rows in splits.items()},
        }, f, ensure_ascii=False, indent=2)


def _scan_split_for_leakage(out_dir: str) -> dict[str, list[tuple[str, str]]]:
    findings: dict[str, list[tuple[str, str]]] = {}
    for name in ("train", "val", "test"):
        items_path = os.path.join(out_dir, name, "items.json")
        if not os.path.isfile(items_path):
            continue
        with open(items_path, encoding="utf-8") as f:
            payload = json.load(f)
        text = json.dumps(payload, ensure_ascii=False)
        hits = redact_mod.scan_for_leakage(text)
        if hits:
            findings[name] = hits
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="Path to JSON file with [{workspace, repo, pr_id}, ...]")
    source.add_argument(
        "--search-current-user-prs",
        dest="search_current_user_prs",
        action="store_true",
        help="Find PRs authored by the authenticated user across --workspace/--repos",
    )
    parser.add_argument("--workspace", default="", help="Bitbucket workspace slug (Mode A)")
    parser.add_argument("--repos", default="", help="Comma-separated repo slugs in --workspace (Mode A)")
    parser.add_argument("--state", default="MERGED", help="PR state filter for Mode A (default: MERGED)")
    parser.add_argument("--max-prs", dest="max_prs", type=int, default=20)
    parser.add_argument("--out_dir", required=True)
    parser.add_argument("--split_ratio", default="7:1:2")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--redact",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run pattern-based redaction before writing (default: on)",
    )
    parser.add_argument("--allow-leakage", action="store_true")
    parser.add_argument("--continue_on_error", action="store_true")
    args = parser.parse_args()

    base_url = os.environ.get("BITBUCKET_URL", _BITBUCKET_DEFAULT_URL).rstrip("/")
    user = os.environ.get("BITBUCKET_USER")
    app_password = os.environ.get("BITBUCKET_APP_PASSWORD")
    if not (user and app_password):
        sys.exit(
            "BITBUCKET_USER and BITBUCKET_APP_PASSWORD must be set. "
            "Create an app password under Bitbucket → Personal settings → App passwords."
        )
    headers = {
        "Authorization": _auth_header(user, app_password),
        "Accept": "application/json",
    }

    if args.search_current_user_prs:
        if not args.workspace or not args.repos:
            sys.exit("--workspace and --repos are required with --search-current-user-prs")
        user_uuid = _get_current_user_uuid(base_url, headers)
        entries: list[dict] = []
        repo_list = [r.strip() for r in args.repos.split(",") if r.strip()]
        for repo in repo_list:
            for pr in _list_pull_requests(
                base_url=base_url, headers=headers,
                workspace=args.workspace, repo=repo,
                author_uuid=user_uuid, state=args.state, page_size=50,
            ):
                entries.append({
                    "workspace": args.workspace,
                    "repo": repo,
                    "pr_id": pr.get("id"),
                    "title_hint": pr.get("title"),
                })
                if len(entries) >= args.max_prs:
                    break
            if len(entries) >= args.max_prs:
                break
        print(f"  discovered {len(entries)} PRs authored by {user}")
        if not entries:
            sys.exit("No PRs found for the authenticated user in the given repos.")
    else:
        with open(args.input, encoding="utf-8") as f:
            entries = json.load(f)
        if not isinstance(entries, list) or not entries:
            sys.exit(f"{args.input} must contain a non-empty JSON array")

    redact_state = redact_mod.RedactionState() if args.redact else None

    items: list[dict] = []
    for entry in entries:
        try:
            item = _build_item(
                base_url=base_url, headers=headers,
                entry=entry, redact_state=redact_state,
            )
        except Exception as exc:  # noqa: BLE001
            if args.continue_on_error:
                print(f"  [skip] {entry}: {exc}", file=sys.stderr)
                continue
            raise
        items.append(item)
        print(f"  fetched {item['id']}: {item['reference_meta'].get('title', '')!r}")

    if not items:
        sys.exit("No items collected — aborting.")

    os.makedirs(args.out_dir, exist_ok=True)
    _write_split(args.out_dir, items, args.split_ratio, args.seed)

    if redact_state is not None:
        mapping_path = os.path.join(args.out_dir, "redaction_mapping.json")
        with open(mapping_path, "w", encoding="utf-8") as f:
            json.dump(redact_state.mapping, f, ensure_ascii=False, indent=2)
        print(f"  wrote redaction mapping → {mapping_path}")

        findings = _scan_split_for_leakage(args.out_dir)
        if findings:
            print("  [leakage] post-redaction scan found remaining identifiers:", file=sys.stderr)
            for split_name, hits in findings.items():
                preview = ", ".join(f"{name}={val!r}" for name, val in hits[:5])
                print(f"    {split_name}: {len(hits)} hits — {preview}", file=sys.stderr)
            if not args.allow_leakage:
                sys.exit(
                    "Aborting because the redaction scan found leaks. "
                    "Inspect the items, extend scripts/redact.py, and re-run. "
                    "Use --allow-leakage to override."
                )
        else:
            print("  [leakage] post-redaction scan: clean")


if __name__ == "__main__":
    main()
