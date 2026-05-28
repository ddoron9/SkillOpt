#!/usr/bin/env python3
"""Ingest Confluence pages into a SkillOpt doc_quality_kr split.

Each Confluence page becomes one training item: the page body is used as the
reference document (the target style), and a short writing prompt is derived
from the page title plus any user-provided context. The trained skill is
expected to reproduce a document of comparable quality without seeing the
reference.

Usage
-----
1. Install dependencies::

       pip install atlassian-python-api beautifulsoup4 html2text

2. Export your Confluence credentials (API token, not the password)::

       export CONFLUENCE_URL="https://yourcompany.atlassian.net/wiki"
       export CONFLUENCE_USER="you@yourcompany.com"
       export CONFLUENCE_TOKEN="atatt..."     # https://id.atlassian.com/manage-profile/security/api-tokens

3. Pick one of the two input modes:

   a) ``--search-current-user-pages`` — auto-discover pages you authored.
      No input JSON required::

          python scripts/ingest_confluence.py \\
              --search-current-user-pages \\
              --redact \\
              --max-pages 16 \\
              --out_dir data/doc_quality_kr_split \\
              --split_ratio 7:1:2

   b) ``--input pages.json`` — explicit list of page ids / URLs::

          [
            {"page_id": "1234567",
             "instruction": "신규 입사자에게 우리 팀 배포 절차를 안내하는 문서를 작성해줘.",
             "audience": "신규 입사자",
             "doc_type": "온보딩 가이드"},
            {"url": "https://yourcompany.atlassian.net/wiki/spaces/ENG/pages/2345/Foo"}
          ]

The script writes ``data/doc_quality_kr_split/{train,val,test}/items.json``.

Redaction
---------
``--redact`` runs the pattern-based redactor in ``scripts/redact.py`` over
each fetched page body and metadata block. Customer / coworker / IP / email
/ ticket / internal-host identifiers are replaced with stable placeholders
(고객사1, 동료1, 내부서버1, …). After writing, the script re-scans the
output for known leakage patterns and aborts if anything is still present.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from typing import Any
from urllib.parse import urlparse

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import redact as redact_mod  # noqa: E402


# ── Small helpers (no hard dep until ingest runs) ────────────────────────────


def _import_confluence_client():
    try:
        from atlassian import Confluence  # type: ignore
    except ImportError as exc:  # pragma: no cover - runtime hint
        raise SystemExit(
            "atlassian-python-api is required. Install with:\n"
            "  pip install atlassian-python-api beautifulsoup4 html2text"
        ) from exc
    return Confluence


def _html_to_markdown(html: str) -> str:
    try:
        import html2text  # type: ignore
    except ImportError as exc:
        raise SystemExit("html2text is required. pip install html2text") from exc
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = False
    converter.ignore_links = False
    return converter.handle(html or "").strip()


def _extract_page_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    # Typical Confluence URLs: .../wiki/spaces/<KEY>/pages/<ID>/<slug>
    for i, part in enumerate(parts):
        if part == "pages" and i + 1 < len(parts):
            candidate = parts[i + 1]
            if candidate.isdigit():
                return candidate
    match = re.search(r"pageId=(\d+)", parsed.query)
    if match:
        return match.group(1)
    return None


def _resolve_page_id(entry: dict) -> str:
    page_id = entry.get("page_id") or entry.get("id")
    if page_id:
        return str(page_id)
    url = entry.get("url") or entry.get("link")
    if url:
        derived = _extract_page_id_from_url(url)
        if derived:
            return derived
    raise ValueError(f"Could not resolve a page id from entry: {entry}")


def _derive_instruction(title: str, doc_type: str, audience: str) -> str:
    audience = (audience or "관련 동료").strip()
    doc_type = (doc_type or "기술 문서").strip()
    return (
        f"{audience}에게 '{title}'에 대한 {doc_type}를 작성해줘. "
        "사내에서 통용되는 자연스러운 한국어로, 결론과 핵심을 앞에 두고 "
        "청자가 한 번에 읽고 행동할 수 있는 분량으로 작성해."
    )


def _build_item(
    *,
    confluence: Any,
    entry: dict,
    include_html: bool,
    redact_state: "redact_mod.RedactionState | None" = None,
) -> dict:
    page_id = _resolve_page_id(entry)
    page = confluence.get_page_by_id(
        page_id,
        expand="body.storage,version,space",
    )
    if not page:
        raise RuntimeError(f"Confluence returned no page for id={page_id}")

    title = entry.get("title") or page.get("title") or f"page-{page_id}"
    html_body = (page.get("body") or {}).get("storage", {}).get("value", "") or ""
    reference_doc = _html_to_markdown(html_body)
    if not reference_doc.strip():
        raise RuntimeError(f"Page {page_id} has empty body")

    audience = entry.get("audience", "")
    doc_type = entry.get("doc_type", "")
    instruction = entry.get("instruction") or _derive_instruction(
        title=title,
        doc_type=doc_type,
        audience=audience,
    )

    meta = {
        "source": "confluence",
        "page_id": page_id,
        "title": title,
        "space_key": (page.get("space") or {}).get("key", ""),
        "version": (page.get("version") or {}).get("number"),
        "url": entry.get("url", ""),
    }

    if redact_state is not None:
        reference_doc = redact_mod.redact(reference_doc, redact_state)
        instruction = redact_mod.redact(instruction, redact_state)
        title = redact_mod.redact(title, redact_state)
        meta["title"] = title
        meta = redact_mod.redact_meta(meta, redact_state)
        meta["redacted"] = True

    item = {
        "id": entry.get("custom_id") or f"confluence-{page_id}",
        "task_type": entry.get("task_type") or "doc",
        "instruction": instruction,
        "audience": audience,
        "doc_type": doc_type,
        "style_hints": entry.get("style_hints", ""),
        "context": entry.get("context", ""),
        "reference_doc": reference_doc,
        "reference_meta": meta,
    }
    if include_html:
        # When redaction is on, do NOT keep raw HTML — it would leak everything
        # the redactor just removed from the markdown body.
        if redact_state is None:
            item["reference_meta"]["html_storage"] = html_body
        else:
            print(
                f"  [warn] dropping --include_html for {page_id}: redaction is on",
                file=sys.stderr,
            )
    return item


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

    manifest = {
        "source": "confluence",
        "total": len(items),
        "split_ratio": split_ratio,
        "seed": seed,
        "counts": {name: len(rows) for name, rows in splits.items()},
    }
    with open(os.path.join(out_dir, "split_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def _discover_current_user_pages(client: Any, max_pages: int) -> list[dict]:
    """Return page entries authored by the authenticated user via CQL."""
    cql = "creator = currentUser() AND type = page ORDER BY lastmodified DESC"
    result = client.cql(cql, limit=max_pages)
    entries: list[dict] = []
    for hit in (result.get("results") or [])[:max_pages]:
        content = hit.get("content") or {}
        page_id = str(content.get("id") or "").strip()
        if not page_id:
            continue
        entries.append({
            "page_id": page_id,
            "title": content.get("title") or "",
        })
    return entries


def _scan_split_for_leakage(out_dir: str) -> dict[str, list[tuple[str, str]]]:
    """Walk the written split files and report any remaining leak patterns."""
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
    source.add_argument("--input", help="Path to JSON file listing Confluence pages")
    source.add_argument(
        "--search-current-user-pages",
        dest="search_current_user_pages",
        action="store_true",
        help="Auto-discover pages authored by the authenticated user via CQL",
    )
    parser.add_argument("--out_dir", required=True, help="Output split directory")
    parser.add_argument("--split_ratio", default="7:1:2", help="train:val:test ratio (default: 7:1:2)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-pages", dest="max_pages", type=int, default=50,
                        help="Cap for --search-current-user-pages (default: 50)")
    parser.add_argument(
        "--redact",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run pattern-based redaction on bodies + metadata before writing (default: on)",
    )
    parser.add_argument(
        "--allow-leakage",
        action="store_true",
        help="Write the split even if the post-redaction scan finds leaks. "
             "Off by default — leakage aborts the write.",
    )
    parser.add_argument("--include_html", action="store_true", help="Also store raw storage HTML for debugging (incompatible with --redact)")
    parser.add_argument("--continue_on_error", action="store_true", help="Skip pages that fail to fetch")
    args = parser.parse_args()

    confluence_url = os.environ.get("CONFLUENCE_URL")
    confluence_user = os.environ.get("CONFLUENCE_USER")
    confluence_token = os.environ.get("CONFLUENCE_TOKEN")
    if not (confluence_url and confluence_user and confluence_token):
        sys.exit(
            "CONFLUENCE_URL, CONFLUENCE_USER, CONFLUENCE_TOKEN must be set. "
            "See `python scripts/ingest_confluence.py --help`."
        )

    Confluence = _import_confluence_client()
    client = Confluence(
        url=confluence_url,
        username=confluence_user,
        password=confluence_token,
        cloud=True,
    )

    if args.search_current_user_pages:
        entries = _discover_current_user_pages(client, args.max_pages)
        print(f"  discovered {len(entries)} pages authored by {confluence_user}")
        if not entries:
            sys.exit("No pages found for the authenticated user.")
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
                confluence=client,
                entry=entry,
                include_html=args.include_html,
                redact_state=redact_state,
            )
        except Exception as exc:  # noqa: BLE001
            if args.continue_on_error:
                print(f"  [skip] {entry}: {exc}", file=sys.stderr)
                continue
            raise
        items.append(item)
        print(f"  fetched {item['id']}: {item['reference_meta']['title']!r}")

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
