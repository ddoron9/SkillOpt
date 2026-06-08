#!/usr/bin/env python3
"""Analyze Claude Code usage history.

Parses Claude Code session transcripts (the ``*.jsonl`` files Claude Code
writes under ``~/.claude/projects/``) and reports:

  1. Skills ranked by how often they were invoked.
  2. (Optional) every tool ranked by use count, including MCP tools.
  3. User requests grouped by shared keywords, so you can see what you
     ask for most often.

Stdlib only -- runs anywhere Python 3.8+ is available. Point it at your
real local history to get meaningful numbers::

    python scripts/skill_usage_analytics.py                 # ~/.claude/projects
    python scripts/skill_usage_analytics.py --tools         # also rank tools
    python scripts/skill_usage_analytics.py --json out.json # machine-readable
    python scripts/skill_usage_analytics.py --dir /path/to/transcripts
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Iterator

# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

SLASH_RE = re.compile(r"^\s*/([a-zA-Z0-9][\w-]*)")
COMMAND_NAME_RE = re.compile(r"<command-name>\s*/?([a-zA-Z0-9][\w-]*)", re.I)
# Wrapper/system text we should never treat as a real user request.
NOISE_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<system-reminder>",
    "<bash-",
    "Caveat:",
)


def iter_transcripts(root: Path) -> Iterator[Path]:
    """Yield every ``*.jsonl`` transcript under ``root``."""
    if root.is_file():
        yield root
        return
    yield from sorted(root.rglob("*.jsonl"))


def iter_records(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def text_blocks(content) -> list[str]:
    """Extract plain-text segments from a message ``content`` field."""
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text")
                if isinstance(txt, str):
                    out.append(txt)
    return out


def is_real_request(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return not stripped.startswith(NOISE_PREFIXES)


# ---------------------------------------------------------------------------
# Keyword grouping for requests
# ---------------------------------------------------------------------------

# Minimal multilingual stopword set (English + common Korean particles/verbs).
STOPWORDS = {
    # English
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for",
    "with", "is", "are", "be", "this", "that", "it", "as", "at", "by", "from",
    "can", "you", "i", "me", "my", "we", "do", "does", "please", "need", "want",
    "make", "add", "use", "get", "let", "should", "would", "could", "will",
    "how", "what", "why", "when", "which", "into", "out", "up", "if", "so",
    "all", "any", "not", "no", "yes", "ok", "okay", "then", "than", "also",
    # Korean (frequent particles / filler)
    "그리고", "근데", "그래서", "해줘", "해줄래", "해주세요", "있음", "있어",
    "있나", "할수", "할", "수", "좀", "내", "나", "너", "이거", "그거", "저거",
    "이걸", "그걸", "에서", "에게", "으로", "하고", "하는", "한", "것", "내용",
    "정리", "관련", "대해", "대한", "또", "더", "잘", "좀더",
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{1,}|[가-힣]{2,}")

# Korean particles/endings to strip from a token's tail (longest first) so that
# "요청사항도"/"요청사항을" both collapse to the stem "요청사항".
KO_SUFFIXES = (
    "으로는", "에서는", "에게서", "이라도", "으로써", "으로서",
    "에서", "으로", "에게", "한테", "처럼", "보다", "까지", "부터",
    "이나", "라도", "든지", "해서", "하고", "하는", "한테", "이란",
    "은", "는", "이", "가", "을", "를", "에", "와", "과", "도", "만",
    "의", "로", "랑", "나", "들", "께", "한", "할", "했", "해", "함",
)


def _strip_ko_suffix(tok: str) -> str:
    for suf in KO_SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 2:
            return tok[: -len(suf)]
    return tok


def keywords(text: str) -> list[str]:
    out = []
    for tok in WORD_RE.findall(text.lower()):
        if "가" <= tok[0] <= "힣":
            tok = _strip_ko_suffix(tok)
        if tok in STOPWORDS or len(tok) < 2:
            continue
        out.append(tok)
    return out


def group_requests(requests: list[str], top_keywords: int) -> list[dict]:
    """Group requests under their most salient shared keyword."""
    freq = collections.Counter()
    for req in requests:
        # count each keyword once per request
        for kw in set(keywords(req)):
            freq[kw] += 1

    groups: list[dict] = []
    for kw, _ in freq.most_common(top_keywords):
        members = [r for r in requests if kw in keywords(r)]
        if len(members) < 1:
            continue
        groups.append({"keyword": kw, "count": len(members), "examples": members[:3]})
    return groups


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyze(paths: Iterable[Path]) -> dict:
    skill_counts: collections.Counter = collections.Counter()
    tool_counts: collections.Counter = collections.Counter()
    requests: list[str] = []
    sessions = 0

    for path in paths:
        sessions += 1
        for rec in iter_records(path):
            rtype = rec.get("type")
            msg = rec.get("message", {}) if isinstance(rec.get("message"), dict) else {}

            if rtype == "assistant":
                for block in msg.get("content", []) or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "?")
                    tool_counts[name] += 1
                    if name == "Skill":
                        skill = (block.get("input") or {}).get("skill")
                        if skill:
                            skill_counts[str(skill)] += 1

            elif rtype == "user":
                for text in text_blocks(msg.get("content")):
                    # Slash-invoked skills show up as user text or command tags.
                    m = COMMAND_NAME_RE.search(text) or SLASH_RE.match(text)
                    if m:
                        skill_counts[m.group(1)] += 1
                    if is_real_request(text):
                        requests.append(text.strip())

    return {
        "sessions": sessions,
        "skill_counts": skill_counts,
        "tool_counts": tool_counts,
        "requests": requests,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def render_text(result: dict, top: int, show_tools: bool) -> str:
    lines: list[str] = []
    skills = result["skill_counts"]
    tools = result["tool_counts"]
    reqs = result["requests"]

    lines.append("=" * 56)
    lines.append("Claude Code Usage Analytics")
    lines.append("=" * 56)
    lines.append(f"Sessions analyzed : {result['sessions']}")
    lines.append(f"User requests     : {len(reqs)}")
    lines.append(f"Distinct skills   : {len(skills)}")
    lines.append("")

    lines.append("## Most-used skills")
    if skills:
        width = max(len(s) for s in skills)
        for rank, (name, n) in enumerate(skills.most_common(top), 1):
            bar = "█" * n
            lines.append(f"{rank:>2}. {name:<{width}}  {n:>4}  {bar}")
    else:
        lines.append("  (no skill/slash-command invocations found)")
    lines.append("")

    if show_tools and tools:
        lines.append("## Most-used tools (incl. MCP)")
        width = max(len(t) for t in tools)
        for rank, (name, n) in enumerate(tools.most_common(top), 1):
            lines.append(f"{rank:>2}. {name:<{width}}  {n:>4}")
        lines.append("")

    lines.append("## Most-requested topics (grouped by keyword)")
    groups = group_requests(reqs, top)
    if groups:
        for rank, g in enumerate(groups, 1):
            lines.append(f"{rank:>2}. [{g['keyword']}] x{g['count']}")
            for ex in g["examples"]:
                snippet = " ".join(ex.split())
                if len(snippet) > 80:
                    snippet = snippet[:77] + "..."
                lines.append(f"      - {snippet}")
    else:
        lines.append("  (no user requests found)")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    default_dir = Path(os.path.expanduser("~/.claude/projects"))
    parser.add_argument("--dir", type=Path, default=default_dir,
                        help="transcript dir or file (default: ~/.claude/projects)")
    parser.add_argument("--top", type=int, default=15, help="rows per ranking")
    parser.add_argument("--tools", action="store_true",
                        help="also rank every tool (Bash, Edit, MCP, ...)")
    parser.add_argument("--json", type=Path, metavar="PATH",
                        help="write machine-readable results to PATH")
    args = parser.parse_args(argv)

    if not args.dir.exists():
        print(f"error: path not found: {args.dir}", file=sys.stderr)
        return 1

    paths = list(iter_transcripts(args.dir))
    if not paths:
        print(f"error: no .jsonl transcripts under {args.dir}", file=sys.stderr)
        return 1

    result = analyze(paths)
    print(render_text(result, args.top, args.tools))

    if args.json:
        payload = {
            "sessions": result["sessions"],
            "skills": result["skill_counts"].most_common(),
            "tools": result["tool_counts"].most_common(),
            "request_groups": group_requests(result["requests"], args.top),
        }
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
