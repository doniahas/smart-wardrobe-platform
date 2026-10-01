"""Conservative plain-text normalization for the
local LLM's conversational chat replies.

The frontend renders a completed chat reply with `el.textContent` (see
`services/web/app/static/chat.js::setChatBubbleText`) - correct and safe
against HTML/script injection, but it also means any literal Markdown the
model emits (e.g. `**Style Recommendations**`) is shown to the user as the
literal `**` characters instead of being rendered as emphasis. The primary
fix is prompt-level: `chat_context.py::PLAIN_TEXT_STYLE_INSTRUCTION` asks
the model not to use Markdown in the first place. This module is the
fallback safety net for when the model doesn't fully comply - conservative
by design, it strips only the exact emphasis/heading *markers* requested by
the task (never the text inside them, never ordinary punctuation), and
never switches rendering to `innerHTML` or introduces a Markdown parser.

**Scope - deliberately narrow.** `normalize_chat_reply` is applied ONLY at
the two conversational reply call sites that return text to a user:
`routers/chat.py`'s synchronous `POST /chat` route, and
`chat_worker.py::process_one_job` (the async `/chat/jobs` path). It is
never applied inside `llm_gateway.py::call_llm` itself, because that
function is also shared by `chat_memory_learning.py`'s structured-JSON
memory-extraction pipeline - that call site needs the model's raw,
unmodified output for its own parser (`memory_extraction.py::
parse_memory_extraction_response`), not a plain-text-normalized version.
Keeping normalization at the two conversational call sites, not inside the
shared gateway, is what keeps this change from interfering with long-term
memory learning, catalog-grounding reply validation (chat_catalog_
grounding.py::validate_grounded_reply, applied after this normalization at
both call sites - see routers/chat.py and chat_worker.py), or prompt-
injection defenses (which govern what goes *into* a prompt, not the model's
own output).
"""
import re

# **bold** / __bold__ -> bold: keeps the text between the markers, drops
# only the markers themselves. Requires non-whitespace immediately inside
# each marker pair and excludes '*'/'_' from the inner content, so this
# never matches a lone "**"/"__" used as ordinary punctuation (e.g. "2**3",
# "well** okay") or spans across an unrelated later pair.
_BOLD_ASTERISK_RE = re.compile(r"\*\*(\S(?:[^*]*\S)?)\*\*")
_BOLD_UNDERSCORE_RE = re.compile(r"__(\S(?:[^_]*\S)?)__")

# A Markdown heading prefix only counts at the very start of a line: 1-6
# '#' characters followed by required whitespace, e.g. "# Title" or
# "## Style Tips:". A mid-sentence or unspaced '#' (a hashtag-like
# "#casual") never matches, so it's left completely untouched.
_HEADING_PREFIX_RE = re.compile(r"^#{1,6}[ \t]+", re.MULTILINE)


def normalize_chat_reply(text: str) -> str:
    """Conservatively strip common Markdown formatting artifacts from one
    LLM conversational reply.

    Only removes: `**`/`__` emphasis markers (keeping their inner text) and
    leading `#`/`##`/... heading markers. Never touches fenced code blocks,
    numbered lists, `-` bullets, or any other ordinary punctuation - nothing
    here is stripped unless it matches one of the two specific patterns
    above. Total (never raises) and idempotent (re-running it on its own
    output is a no-op), so it's always safe to apply once, unconditionally,
    to any string the LLM returns.
    """
    if not text:
        return text
    result = _BOLD_ASTERISK_RE.sub(r"\1", text)
    result = _BOLD_UNDERSCORE_RE.sub(r"\1", result)
    result = _HEADING_PREFIX_RE.sub("", result)
    return result
