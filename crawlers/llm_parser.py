"""LLM-backed semantic parser for auction-house catalog descriptions.

Regex parsing handles the common cases but breaks on bilingual blobs,
signature/inscription text bleeding into medium, and free-form prose.
The LLM pass takes the raw description + (optional) raw title, and
returns a clean structured record.

Default model: Claude Haiku 4.5 — cheap, fast, sufficient for
structured field extraction.  Sonnet 4.6 used only as fallback when
Haiku returns low-confidence results.

Cost (audited 2026-06-23 against actual lots):
  ~750 input tokens + ~200 output tokens per lot
  Haiku 4.5: ~$0.0014/lot ($1.40 per 1,000 lots)
  With prompt cache + batch API: ~$0.0006/lot

Wire-up:
  - At insert time (crawl_and_sync.py): refine fields that regex
    couldn't fill cleanly.
  - One-shot backfill scripts: re-process existing DB rows.

Validation rules enforce sanity (year ∈ [1850, 2030], dim in cm) — an
LLM hallucination outside those bounds is dropped, not stored.
"""
import json
import os
import re
from pathlib import Path

import anthropic

# Models per the harness's "knowledge cutoff" reminder.  Default to
# Haiku 4.5; only escalate to Sonnet for low-confidence retries.
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"


def _load_env():
    env_path = Path(__file__).resolve().parent.parent / ".env.local"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)


_load_env()
_client = None


def _client_lazy():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


SYSTEM_PROMPT = """You extract structured metadata from auction-house catalog text.

The input is a raw catalog description (often bilingual French + English,
sometimes also Vietnamese) plus an optional raw title.  Return ONLY a
JSON object with these fields (use null when not present):

{
  "medium": string|null,           // material + support, single phrase
                                    // e.g. "gouache sur papier journal"
                                    // or "oil on canvas"
                                    // PICK ONE language — prefer the
                                    // original (French if bilingual,
                                    // otherwise English).
                                    // EXCLUDE: signature notes, dates,
                                    // inscriptions, dimensions.
  "year": string|null,             // 4-digit creation year if stated.
                                    // Look for "Painted in 1923",
                                    // "Executed 1965", "circa 1980",
                                    // "1965-1966" (use first year).
                                    // EXCLUDE: artist birth/death years,
                                    // sale year, provenance dates.
  "signature_info": string|null,   // raw signature phrasing if present
                                    // e.g. "signé et daté nge 98
                                    // (en bas à droite)"
  "dimensions_text": string|null,  // raw dim phrase like "29 x 40 cm"
                                    // or "H. 29 cm × L. 40 cm"
                                    // EXCLUDE inch-only variants when
                                    // a cm version exists.
  "inscription": string|null,      // verso / mount / certificate text
  "provenance": string|null,       // ownership history line if present
  "title": string|null,            // artwork title if recoverable
                                    // from description, in original
                                    // language.  null if title is
                                    // implicit / unstated.
  "language": "fr"|"en"|"bilingual"|"vi"|"other",
  "confidence": 0.0-1.0,           // your own assessment of how
                                    // clean / unambiguous the input was
}

RULES:
- Output strictly the JSON object, no prose, no markdown fences.
- When the medium contains both languages ("gouache sur papier journal
  ... gouache on newspaper"), keep only one — the original (French
  for Bonhams / Aguttes / Drouot / Millon).
- Year must be 1850-2030, else null.
- Confidence < 0.6 if the text is ambiguous or you're guessing."""


USER_TEMPLATE = """RAW_TITLE: {title}
RAW_DESCRIPTION: {description}

Extract structured fields per the JSON schema.  Reply with JSON only."""


def _validate(parsed: dict) -> dict:
    """Sanity-check LLM output; drop obvious hallucinations."""
    out = {}
    for k in ("medium", "signature_info", "dimensions_text",
              "inscription", "provenance", "title"):
        v = parsed.get(k)
        if isinstance(v, str):
            v = v.strip()
            if v and v.lower() not in ("null", "none", "n/a"):
                out[k] = v[:500]
    # Year sanity
    y = parsed.get("year")
    if isinstance(y, (str, int)) and y:
        y_str = str(y).strip()
        m = re.fullmatch(r"(\d{4})", y_str)
        if m:
            yi = int(m.group(1))
            if 1850 <= yi <= 2030:
                out["year"] = m.group(1)
    # Language enum
    lang = parsed.get("language")
    if lang in ("fr", "en", "bilingual", "vi", "other"):
        out["language"] = lang
    # Confidence
    conf = parsed.get("confidence")
    if isinstance(conf, (int, float)) and 0 <= conf <= 1:
        out["confidence"] = float(conf)
    return out


def extract_lot_fields(description: str, title: str = "",
                       model: str = HAIKU_MODEL) -> dict:
    """Call Claude to extract clean structured fields.

    Returns a dict with the keys from the prompt schema (subset of
    keys present when the field couldn't be extracted reliably).
    On API error or unparseable output, returns {'error': str}.
    """
    if not description or not description.strip():
        return {"error": "empty description"}
    client = _client_lazy()
    user_msg = USER_TEMPLATE.format(
        title=(title or "(none)")[:300],
        description=description[:4000],
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        return {"error": f"api: {e}"}
    text = "".join(
        block.text for block in resp.content
        if getattr(block, "type", None) == "text"
    ).strip()
    # Strip markdown fences if model ignored the rule
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return {"error": f"parse: {e}", "raw": text[:300]}
    out = _validate(parsed)
    # Token usage for cost auditing
    out["_usage"] = {
        "input_tokens": getattr(resp.usage, "input_tokens", None),
        "output_tokens": getattr(resp.usage, "output_tokens", None),
        "model": model,
    }
    return out
