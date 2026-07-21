"""Robust JSON extraction for provider structured-output responses.

Reasoning-tier models occasionally wrap their JSON object in extra text inside a
response's aggregated ``output_text``/``text`` field — leading reasoning or a
trailing note — so a bare ``json.loads`` raises ``JSONDecodeError: Extra data``.
Unguarded, that exception takes down the whole run (observed live 2026-07-20: a
GPT co-lead tool review returned a valid object followed by extra content, at
``char 1076``). Take the first complete JSON value and log whatever surrounds it,
so surplus content is visible rather than fatal.

The send side is unaffected: each adapter keeps its own strict-schema builder
(OpenAI/Gemini strict modes reject ``format: path`` and ``default``, which the
SDK's own Pydantic conversion keeps — so the hand-rolled sanitizers stay). This
module only hardens the receive side.
"""

import json
import logging

_log = logging.getLogger(__name__)


def loads_first_json_object(text: str):
    """Parse the first complete JSON value in ``text``; return None if none exists.

    Tolerates content before or after the value: skips any leading prose to the
    first ``{``/``[`` and ignores a trailing tail, logging a warning when either
    is dropped. Returns the parsed object/list, or ``None`` when no JSON value can
    be found — the caller's structured-retry loop then re-prompts rather than
    crashing. A clean single-object response parses exactly as ``json.loads``
    would, so the happy path is unchanged.
    """
    if not text:
        return None
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        start = 0
    else:
        candidates = [i for i in (stripped.find("{"), stripped.find("[")) if i != -1]
        if not candidates:
            _log.warning("structured output contained no JSON value; caller will retry")
            return None
        start = min(candidates)
    try:
        obj, end = json.JSONDecoder().raw_decode(stripped, start)
    except json.JSONDecodeError as exc:
        _log.warning("structured output was not parseable JSON (%s); caller will retry", exc)
        return None
    trailing = stripped[end:].strip()
    if start or trailing:
        _log.warning(
            "structured output carried extra content around the JSON value "
            "(%d leading, %d trailing chars); used the first complete value",
            start, len(trailing),
        )
    return obj
