"""HTML → structured text parser with heading preservation and exact offsets.

Responsibility (single owner of the parsed-text concern, ARCHITECTURE §6.1):
extract a stable, readable text stream from a page's HTML while
  1. preserving the heading chain (h1–h6) with **exact offsets** into the
     stored text (M2 evidence verification will locate quotes at these
     positions, so offsets must be exact against the stored text);
  2. stripping ``script``/``style``/``noscript``/``template``/``iframe``/
     ``svg`` entirely, before any text assembly;
  3. stripping hidden content per the approved rules: ``hidden`` attribute,
     ``aria-hidden="true"``, ``display:none`` / ``visibility:hidden``
     inline styles, and well-known visually-hidden utility classes
     (``sr-only``/``offscreen``/``visually-hidden``/``hidden``);
  4. normalising whitespace deterministically so the content hash is
     stable across repeated ingestions.

Inputs:  raw HTML string.
Outputs: ``ParsedPage(title, text, headings)``; ``PARSER_VERSION`` is
stamped onto every snapshot so a future parser upgrade is detectable.
Failure modes: unparseable/empty HTML raises ``TextExtractionError``.
No network. No AI. Deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, NavigableString, Tag

PARSER_VERSION = "text_extract_v1"

_WS_RUN = re.compile(r"\s+")
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_HIDDEN_CLASSES = {"sr-only", "offscreen", "visually-hidden", "hidden"}
_STRIPPED_TAGS = ("script", "style", "noscript", "template", "iframe", "svg")
_INLINE_TAGS = {"a", "span", "em", "strong", "b", "i", "code", "small", "sup", "sub", "u"}
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")


class TextExtractionError(ValueError):
    """The HTML could not be parsed into the structured text contract."""


@dataclass
class HeadingSpan:
    level: int
    text: str
    start_offset: int
    end_offset: int


@dataclass
class ParsedPage:
    title: str | None
    text: str
    headings: list[HeadingSpan] = field(default_factory=list)


def _normalise_ws(text: str) -> str:
    """Collapse every whitespace run (incl. NBSP) into a single space."""
    return _WS_RUN.sub(" ", text.replace("\u00a0", " ")).strip()


def _is_hidden(el: Tag) -> bool:
    """True when the element itself is visibly hidden per the approved rules."""
    if el.has_attr("hidden"):
        return True
    aria = el.get("aria-hidden")
    if isinstance(aria, str) and aria.strip().lower() == "true":
        return True
    style = el.get("style")
    if isinstance(style, str) and _HIDDEN_STYLE.search(style):
        return True
    klass = el.get("class")
    if isinstance(klass, list):
        if any(c in _HIDDEN_CLASSES for c in klass):
            return True
    elif isinstance(klass, str) and klass in _HIDDEN_CLASSES:
        return True
    return False


def _prepare_soup(html: str) -> tuple[BeautifulSoup, Tag]:
    """Parse, strip script/style/template nodes and hidden elements."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.body
    if body is None:
        raise TextExtractionError("html has no <body>; cannot extract text")

    for node in list(soup.find_all(list(_STRIPPED_TAGS))):
        node.decompose()

    # Hidden elements: decompose the top-most hidden nodes; two nested hidden
    # nodes each match, so skip anything already removed in this pass.
    for el in list(body.descendants):
        if isinstance(el, Tag) and not el.decomposed and _is_hidden(el):
            el.decompose()
    return soup, body


def _assemble_text_stream(root: Tag, parts: list[tuple[str, int | None]], headings: list[tuple[int, str]]) -> None:
    """Depth-first assembly of ``(text, heading_level_or_None)`` parts.

    A part is one visual line: an inline run inside a block, a heading, or
    the text of a block leaf (p/li/td/caption/...). Blocks concatenate with
    newlines at join time; parts stay small so heading offsets are exact.
    """
    inline_run: list[str] = []

    def flush_inline() -> None:
        text = _normalise_ws(" ".join(inline_run))
        inline_run.clear()
        if text:
            parts.append((text, None))

    for child in root.children:
        if isinstance(child, NavigableString):
            if _normalise_ws(str(child)) or str(child).strip() == "":
                # keep whitespace tokens for word joins; empties are noise
                inline_run.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in _STRIPPED_TAGS:
            continue
        if name == "br":
            inline_run.append(" ")
            continue
        if name in _INLINE_TAGS:
            inline_run.append(child.get_text(" ", strip=False))
            continue
        if name in _HEADING_TAGS:
            flush_inline()
            heading_text = _normalise_ws(child.get_text(" ", strip=False))
            if heading_text:
                level = int(name[1])
                headings.append((level, heading_text))
                parts.append((heading_text, level))
            continue
        # Block-level child (p, li, td, caption, div, section, ...): recurse.
        flush_inline()
        _assemble_text_stream(child, parts, headings)

    flush_inline()


def parse(html: str) -> ParsedPage:
    """Parse one HTML document. Deterministic; raises on unusable HTML."""
    if not isinstance(html, str) or not html.strip():
        raise TextExtractionError("html payload is empty")
    soup, body = _prepare_soup(html)
    title_node = soup.find("title")
    title = _normalise_ws(title_node.get_text(strip=True)) or None if title_node else None

    parts: list[tuple[str, int | None]] = []
    headings: list[tuple[int, str]] = []
    _assemble_text_stream(body, parts, headings)
    if not parts:
        raise TextExtractionError("no readable text after stripping script/style/hidden content")

    # Second pass: exact offsets of every part in the assembled text stream.
    # Part separators are single newlines; each part spans [start, end).
    stream: list[str] = []
    heading_spans: list[HeadingSpan] = []
    cursor = 0
    for text, level in parts:
        if stream:
            cursor += 1  # the newline separator
        start = cursor
        end = start + len(text)
        cursor = end
        stream.append(text)
        if level is not None:
            heading_spans.append(HeadingSpan(level=level, text=text, start_offset=start, end_offset=end))
    final_text = "\n".join(stream)

    # Cheap determinism assertion: heading text must live at its offsets.
    for span in heading_spans:
        if final_text[span.start_offset : span.end_offset] != span.text:  # pragma: no cover
            raise TextExtractionError("internal offset drift — refusing to emit mismatching offsets")

    return ParsedPage(title=title, text=final_text, headings=heading_spans)
