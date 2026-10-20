"""AI出力・外部テキストのサニタイズ（生HTML禁止・リンク強化）。"""
from __future__ import annotations

import re

import bleach

_ALLOWED_TAGS = [
    "p", "br", "strong", "em", "ul", "ol", "li",
    "code", "pre", "blockquote",
    "table", "thead", "tbody", "tr", "th", "td",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "hr",
]
_ALLOWED_ATTRS = {"a": ["href", "title"]}


def sanitize_markdown_html(text: str) -> str:
    cleaned = bleach.clean(
        text, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True
    )
    return re.sub(r"<a([^>]*?)>", _harden, cleaned)


def _harden(m: re.Match[str]) -> str:
    attrs = m.group(1)
    if "noopener" in attrs:
        return m.group(0)
    return f'<a{attrs} rel="noopener noreferrer">'