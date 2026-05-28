"""Pattern-based redaction for Confluence-sourced training data.

Purpose
-------
Replace personal / company-internal identifiers with consistent placeholder
tokens while preserving the surrounding Korean writing style. Designed for
the doc_quality_kr benchmark, where the goal is to learn natural Korean
writing patterns — not specific facts.

Design choices
--------------
- Replacements are stateful so the same identifier always maps to the same
  placeholder across all documents (e.g. "현대중공업" → "고객사1" everywhere).
- Categories are ordered (longest / most specific first) to avoid partial
  overlap (e.g. an email domain shouldn't be matched as a bare hostname).
- The replacement strings are plain Korean / English nouns ("고객사1",
  "동료1") rather than `[REDACTED]` brackets, so the redacted text still
  reads like natural prose.
- The module exposes `redact(text, state)` so callers can apply the same
  mapping table across many documents in one batch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Hardcoded identifier lists (extend as needed) ───────────────────────────
#
# These mirror what we expect in the source corpus. They are pre-seeded so the
# mapping is stable across runs (and reviewable). Anything not listed here but
# matched by a generic pattern (IP, email, ...) is auto-numbered as it is
# encountered.

CUSTOMER_NAMES: tuple[str, ...] = (
    "현대중공업",
    "삼성증권",
    "한국투자미국배당귀족증권자투자신탁H",
    "한국투자",
)

# Classification society / standard body names sometimes appear as bare
# acronyms ("BV", "LR", "NK", "KR", "DNV"). Matching them as plain substrings
# is dangerous (e.g. "KR" inside a regex). Instead, require word boundaries
# and only at explicit ALL-CAPS positions or after a delimiter.
ACRONYM_ORGS: tuple[str, ...] = (
    "DNV",
    "BV",
    "LR",
    "NVCA",
    "ISO9001",
    "NK",
)
# "KR" is too ambiguous to match generically — leave it alone unless paired
# with "_Fund" or similar disambiguating tokens.

COWORKER_NAMES: tuple[str, ...] = (
    "이지훈",
    "김도이",
    "김윤진",
)

INTERNAL_HOST_NAMES: tuple[str, ...] = (
    "crowdworksinc.atlassian.net",
)

INTERNAL_GIT_ORGS: tuple[str, ...] = (
    "crowdworks_dev",
)

INTERNAL_SYSTEM_NAMES: tuple[str, ...] = (
    "knowledge_compiler",
    "kc-backend",
)

# ── Regex helpers ───────────────────────────────────────────────────────────

# RFC 1918 private-network ranges + common in-house subnets.
_IP_PRIVATE = re.compile(
    r"\b("
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r")\b"
)

_EMAIL = re.compile(r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")

_KOREAN_PHONE = re.compile(r"\b01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}\b")

# Ticket IDs follow PROJECT-NUM. We keep the project prefix list narrow so
# we don't accidentally redact generic patterns like "ISO-9001".
_TICKET = re.compile(r"\b(KCP|FA|JIRA)-\d{1,5}\b")

# Avatar URLs from Atlassian are pure noise; drop them entirely.
_AVATAR_URL = re.compile(
    r"https?://crowdworksinc\.atlassian\.net/wiki/aa-avatar/\S+"
)

# Smartlink wrappers around internal URLs — strip the tag, keep nothing.
_SMARTLINK_INTERNAL = re.compile(
    r"<custom\s+data-type=\"smartlink\"[^>]*>"
    r"https?://crowdworksinc\.atlassian\.net/\S+?"
    r"</custom>"
)

# Author/version metadata blocks coming out of html2text sometimes embed the
# avatar URL plus the display name. Keep the display name handling to the
# coworker map below — just clear the avatar wrapping here.

# Internal absolute paths like "/home/kc/kc-backend/" should be generalized.
_INTERNAL_HOME_PATH = re.compile(r"/home/kc(/[\w./-]*)?")

# Generic "by. <name>, <name>" attribution lines.
_BYLINE = re.compile(r"by\.\s*[가-힣A-Za-z, ]+")


# ── State ───────────────────────────────────────────────────────────────────


@dataclass
class RedactionState:
    """Carries the stable identifier→placeholder mapping across documents."""

    customer_idx: int = 0
    coworker_idx: int = 0
    ip_idx: int = 0
    email_idx: int = 0
    phone_idx: int = 0
    ticket_idx: int = 0
    mapping: dict[str, str] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)

    def _assign(self, original: str, category: str, idx_attr: str) -> str:
        if original in self.mapping:
            return self.mapping[original]
        next_idx = getattr(self, idx_attr) + 1
        setattr(self, idx_attr, next_idx)
        placeholder = f"{category}{next_idx}"
        self.mapping[original] = placeholder
        return placeholder

    def map_customer(self, name: str) -> str:
        return self._assign(name, "고객사", "customer_idx")

    def map_coworker(self, name: str) -> str:
        return self._assign(name, "동료", "coworker_idx")

    def map_ip(self, ip: str) -> str:
        return self._assign(ip, "내부서버", "ip_idx")

    def map_email(self, email: str) -> str:
        return self._assign(email, "이메일", "email_idx")

    def map_phone(self, phone: str) -> str:
        return self._assign(phone, "전화", "phone_idx")

    def map_ticket(self, ticket: str) -> str:
        return self._assign(ticket, "TICKET-", "ticket_idx")


# ── Core redactor ───────────────────────────────────────────────────────────


def _replace_literal(text: str, needle: str, replacement: str) -> str:
    if not needle or needle not in text:
        return text
    return text.replace(needle, replacement)


def redact(text: str, state: RedactionState) -> str:
    """Apply the full redaction pipeline to a single document.

    The same `state` should be reused across documents to keep placeholders
    consistent (e.g. customer 1 in doc A stays customer 1 in doc B).
    """
    if not text:
        return text

    # 1. Drop avatar URLs and internal smartlinks first — they're noise.
    text = _AVATAR_URL.sub("", text)
    text = _SMARTLINK_INTERNAL.sub("<내부링크>", text)

    # 2. Strip "by. <names>" attribution lines.
    text = _BYLINE.sub("by. <작성자>", text)

    # 3. Customer / company names (longest first to avoid partial overlap).
    for name in sorted(CUSTOMER_NAMES, key=len, reverse=True):
        if name in text:
            text = _replace_literal(text, name, state.map_customer(name))

    # 4. Acronym orgs — only when surrounded by word boundaries.
    for acronym in sorted(ACRONYM_ORGS, key=len, reverse=True):
        pattern = re.compile(rf"\b{re.escape(acronym)}\b")
        if pattern.search(text):
            replacement = state.map_customer(acronym)
            text = pattern.sub(replacement, text)

    # 5. Coworker names.
    for name in sorted(COWORKER_NAMES, key=len, reverse=True):
        if name in text:
            text = _replace_literal(text, name, state.map_coworker(name))

    # 6. Internal hosts / git orgs / system names.
    for host in INTERNAL_HOST_NAMES:
        text = _replace_literal(text, host, "<COMPANY>.atlassian.net")
    for org in INTERNAL_GIT_ORGS:
        text = _replace_literal(text, org, "<ORG_GIT>")
    for system in INTERNAL_SYSTEM_NAMES:
        text = _replace_literal(text, system, "<INTERNAL_SYS>")

    # 7. Internal absolute paths.
    text = _INTERNAL_HOME_PATH.sub(
        lambda m: f"/home/<user>{m.group(1) or ''}",
        text,
    )

    # 8. Private IPs.
    text = _IP_PRIVATE.sub(lambda m: state.map_ip(m.group(1)), text)

    # 9. Emails / phones.
    text = _EMAIL.sub(lambda m: state.map_email(m.group(0)), text)
    text = _KOREAN_PHONE.sub(lambda m: state.map_phone(m.group(0)), text)

    # 10. Ticket IDs.
    text = _TICKET.sub(lambda m: state.map_ticket(m.group(0)), text)

    return text


def redact_meta(meta: dict, state: RedactionState) -> dict:
    """Strip identifying fields from a Confluence page metadata dict."""
    cleaned = dict(meta)
    # Drop avatar URL and author display info entirely.
    cleaned.pop("author", None)
    cleaned.pop("avatarUrls", None)
    # Internal page URL → generic placeholder. Keep page_id only as a relative
    # opaque token so the operator can still cross-reference locally, but the
    # external URL leaks the host name.
    url = str(cleaned.get("url", "") or "")
    if url:
        cleaned["url"] = redact(url, state)
    return cleaned


# ── Standalone leakage scan (post-redaction safety check) ───────────────────


_LEAKAGE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("private_ip", _IP_PRIVATE),
    ("email", _EMAIL),
    ("korean_phone", _KOREAN_PHONE),
    ("ticket_id", _TICKET),
    ("internal_host", re.compile(r"crowdworksinc\.atlassian\.net")),
    ("internal_git", re.compile(r"crowdworks_dev")),
    ("internal_home", re.compile(r"/home/kc(?![<])")),
)


def scan_for_leakage(text: str) -> list[tuple[str, str]]:
    """Return a list of (pattern_name, matched_text) for any remaining hits."""
    findings: list[tuple[str, str]] = []
    for name, pattern in _LEAKAGE_PATTERNS:
        for match in pattern.finditer(text):
            findings.append((name, match.group(0)))
    return findings
