"""Text mined from library source is untrusted data (invariant 10 in AGENTS.md, DT-CORE-08).

DepTime's answers are read by coding agents, so text from library source reaches an agent's context:
deprecation messages, replacement evidence, signature defaults and annotations. The covered packages are well
known, but one compromised release could put text aimed at that agent into a deprecation warning. Every such
field passes through `untrusted_text` on its way out (`models.py`): characters that render as nothing or
reorder text are removed, whitespace is collapsed, length is capped, images and HTML that would fetch a URL
when rendered are defused, and text that reads like instructions to an assistant is withheld, along with any
replacement mined from it (`query.py`).

The instruction patterns are a speed bump, not a guarantee: the field descriptions also tell agents that the
text is data to quote, never to follow.
"""

from __future__ import annotations

import re
import unicodedata

MAX_CHARS = 400  # deprecation text is already cut at 400 characters when it is mined
MAX_CODE_CHARS = 5000  # signature defaults and annotations: the longest real one is 4,624
WITHHELD = "[withheld: this library text reads like instructions to an AI assistant]"

# None of these matches any of the 60,097 distinct texts mined into the index on 2026-11-02.
_INSTRUCTION_RE = re.compile(
    "|".join(
        (
            # "Ignore all previous instructions"
            r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}"
            r"\b(?:previous|prior|above|earlier|all|any|your|these|the)\b[^.\n]{0,30}"
            r"\b(?:instructions?|prompts?|rules|guidelines|directions)\b",
            # "You are now an AI assistant ..."
            r"\byou\s+are\s+(?:now\s+)?(?:an?\s+|the\s+)?(?:ai|assistant|agent|language\s+model|llm|chatbot)\b",
            r"\bas\s+an?\s+(?:ai|assistant|language\s+model)\b",
            # Chat-transcript and tool-protocol markup: "</tool_result>", "<system>", "<IMPORTANT>", "<|im_start|>"
            r"</?\s*(?:system|assistant|user|human|tool_?(?:call|result|use)|function_?(?:calls?|results?)"
            r"|instructions?|important)\b[^>\n]{0,40}>",
            r"<\|\s*im_(?:start|end)",
            # Shell commands an agent might run: "curl ... | sh", "rm -rf ~"
            r"\b(?:curl|wget)\b[^|\n]{0,200}\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b",
            r"\brm\s+-[a-z]*r[a-z]*\s+[~/]",
            # Addressed to the agent, or about its user
            r"\b(?:do\s+not|don't|never)\s+(?:tell|inform|mention|reveal|show)\b[^.\n]{0,30}\buser\b",
            r"\b(?:tell|ask|instruct|convince)\s+the\s+user\s+to\b",
            r"\b(?:assistants?|agents?|ai|llms?|models?)\s+(?:must|should)\s+(?:now\s+)?"
            r"(?:run|execute|install|delete|send|call|ignore|always|never)\b",
            r"\b(?:run|execute)\s+(?:the\s+following|this)\s+(?:command|shell|script|code)\b",
        )
    ),
    re.IGNORECASE,
)

# Removed: format characters (bidi overrides, zero-width characters, Unicode tag characters), private-use,
# surrogate and unassigned code points, and variation selectors, which can carry hidden bytes after a visible
# character. Control characters (newlines, tabs, NUL, ESC) become spaces.
_REMOVED = {"Cf", "Co", "Cs", "Cn"}
# Unicode's other default-ignorable characters, which render as nothing but aren't format characters: the
# combining grapheme joiner, Hangul fillers, Khmer inherent vowels and Mongolian variation selectors.
_IGNORABLE = frozenset("\u034f\u115f\u1160\u17b4\u17b5\u180b\u180c\u180d\u180f\u3164\uffa0")

# Markdown images (`![a](url)`, also after more `!`) and HTML attributes that fetch a URL when a client renders the
# text: both can send data to a server. Images become links; such a tag's `<` is escaped, so it shows as text.
_IMAGE_RE = re.compile(r"!+\[")
_FETCHING_TAG_RE = re.compile(
    r"<(?=[A-Za-z][\w:-]*\s[^<>]*?\b(?:src|srcset|href|xlink:href|data|poster|background|style|action"
    r"|formaction)\s*=)",
    re.IGNORECASE,
)


def _visible(c: str) -> str:
    if 0xFE00 <= ord(c) <= 0xFE0F or 0xE0100 <= ord(c) <= 0xE01EF or c in _IGNORABLE:
        return ""
    category = unicodedata.category(c)
    return "" if category in _REMOVED else " " if category == "Cc" else c


def instruction_like(text: str | None) -> bool:
    """True for text that addresses an assistant rather than describing an API (checked after cleaning and
    NFKC normalization, so zero-width characters and fullwidth letters don't hide it)."""
    if not text:
        return False
    cleaned = " ".join("".join(map(_visible, text)).split())
    return _INSTRUCTION_RE.search(unicodedata.normalize("NFKC", cleaned)) is not None


def untrusted_text(text: str | None, limit: int = MAX_CHARS) -> str | None:
    """`text` as an answer may carry it: visible characters only, whitespace collapsed, capped at `limit`,
    Markdown images turned into plain links and HTML that fetches a URL shown as text, or `WITHHELD` if it reads
    like instructions."""
    if text is None:
        return None
    if instruction_like(text):
        return WITHHELD
    cleaned = " ".join("".join(map(_visible, text)).split())
    cleaned = _FETCHING_TAG_RE.sub("&lt;", _IMAGE_RE.sub("[", cleaned))
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def untrusted_code(text: str | None) -> str | None:
    """A signature default or annotation: cleaned like other text, with room for long type annotations."""
    return untrusted_text(text, MAX_CODE_CHARS)


def untrusted_detail(value):
    """Every string in an event's detail (messages, parameter names, alternatives), cleaned."""
    if isinstance(value, str):
        return untrusted_text(value)
    if isinstance(value, dict):
        return {k: untrusted_detail(v) for k, v in value.items()}
    if isinstance(value, list):
        return [untrusted_detail(v) for v in value]
    return value
