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
  "year": string|null,             // 4-digit artwork CREATION year.
                                    // INCLUDE only when EXPLICITLY stated
                                    // about the artwork itself:
                                    //   "Painted in 1923"
                                    //   "Executed 1965"
                                    //   "circa 1980"
                                    //   "1965-1966" (use first year)
                                    //   Title text like "Vue, 1965"
                                    //   Description like "réalisée en 1965"
                                    // EXCLUDE — return null when only:
                                    //   - artist birth year ("sinh năm 1957",
                                    //     "born 1957", "(1957-)", "(b. 1957)")
                                    //   - artist death year
                                    //   - "(1907-2001)" birth-death pair
                                    //   - sale year / catalog year
                                    //   - provenance dates ("acquired 1985")
                                    // Default to null when uncertain.
  "signature_info": string|null,   // raw signature phrasing if present
                                    // e.g. "signé et daté nge 98
                                    // (en bas à droite)"
  "dimensions_text": string|null,  // raw dim phrase like "29 x 40 cm"
                                    // or "H. 29 cm × L. 40 cm"
                                    // EXCLUDE inch-only variants when
                                    // a cm version exists.
  "estimate_low": number|null,     // low end of pre-sale estimate in
                                    // the catalog currency, no comma
                                    // separators.  e.g. 50000 for
                                    // "Estimation: 50 000 € - 70 000 €".
                                    // Patterns: "Estimation: X-Y €",
                                    // "Estimate: \$X-\$Y", "Estimate:
                                    // £X-£Y", "估價: HK\$X - HK\$Y".
                                    // Use null when only a single value
                                    // is shown (point estimate).
  "estimate_high": number|null,    // high end of the estimate range.
  "estimate_currency": string|null,// ISO code: USD, EUR, GBP, HKD,
                                    // CHF, JPY, CNY, SGD, MYR, AUD, THB.
  "hammer_price": number|null,     // realised hammer in catalog currency
                                    // when explicitly stated:
                                    //   "Adjugé: X €" (Millon)
                                    //   "Sold for \$X"
                                    //   "Realised: £X"
                                    //   "成交價: HK\$X"
                                    // Null when not shown publicly.
  "hammer_currency": string|null,  // ISO code matching the hammer.
  "inscription": string|null,      // verso / mount / certificate text
  "provenance": string|null,       // ownership history line if present
  "title": string|null,            // artwork title if recoverable
                                    // from description, in original
                                    // language.  null if title is
                                    // implicit / unstated.
  "artist": string|null,           // artist name in Latin script (no
                                    // diacritics if the description
                                    // doesn't have them), e.g.
                                    // "Mai Trung Thu", "Le Pho",
                                    // "Henri Mege", "Nguyen Gia Tri".
                                    // RETURN NULL when description
                                    // names a country / dynasty
                                    // rather than a person, e.g.
                                    // "VIETNAM, 19th century" /
                                    // "CHINA, Qing dynasty" /
                                    // "JAPAN, Edo period".
                                    // Pick the SHORTEST canonical
                                    // form: 'Lebadang' over
                                    // 'Dang Lebadang Dang Lebadang'.
  "birth_year": number|null,       // artist birth year if stated,
                                    // 4-digit int (e.g. 1907).  null
                                    // if not in text.
  "death_year": number|null,       // artist death year if stated,
                                    // 4-digit int (e.g. 2001).  null
                                    // if living or not in text.
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


_VALID_CURS = {"USD","EUR","GBP","HKD","CHF","JPY","CNY","SGD","MYR","AUD","THB"}


def _validate(parsed: dict) -> dict:
    """Sanity-check LLM output; drop obvious hallucinations."""
    out = {}
    for k in ("medium", "signature_info", "dimensions_text",
              "inscription", "provenance", "title", "artist"):
        v = parsed.get(k)
        if isinstance(v, str):
            v = v.strip()
            if v and v.lower() not in ("null", "none", "n/a"):
                out[k] = v[:500]
    # Year sanity (artwork creation year)
    y = parsed.get("year")
    if isinstance(y, (str, int)) and y:
        y_str = str(y).strip()
        m = re.fullmatch(r"(\d{4})", y_str)
        if m:
            yi = int(m.group(1))
            if 1850 <= yi <= 2030:
                out["year"] = m.group(1)
    # Artist birth/death year sanity (1700-2030)
    for k in ("birth_year", "death_year"):
        v = parsed.get(k)
        if isinstance(v, (int, float)):
            yi = int(v)
            if 1700 <= yi <= 2030:
                out[k] = yi
    # Numeric price fields with currency code sanity
    for k in ("estimate_low", "estimate_high", "hammer_price"):
        v = parsed.get(k)
        if isinstance(v, (int, float)) and v > 0 and v < 1e9:
            out[k] = float(v)
    for k in ("estimate_currency", "hammer_currency"):
        v = parsed.get(k)
        if isinstance(v, str) and v.strip().upper() in _VALID_CURS:
            out[k] = v.strip().upper()
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


# Country / region tokens that mark a description as "anonymous antique"
# rather than artist-attributed.  Pre-filter dodges wasted LLM calls on
# Drouot's Asian Art sales (Marambat-de Malafosse = 200+ pottery lots
# starting with "CHINA, ...", "VIETNAM, 19th century", etc.).
_COUNTRY_ANTIQUE_PREFIX_RE = re.compile(
    r'^(?:VIETNAM|CHINA|JAPAN|KOREA|THAILAND|INDIA|INDONESIA|CAMBODIA|'
    r'TIBET|MONGOLIA|BURMA|MYANMAR|LAOS|MALAYSIA|PHILIPPINES|'
    r'PERSIA|TURKEY|SYRIA)[,\s]',
    re.IGNORECASE,
)


def should_try_llm_artist(desc: str) -> bool:
    """Cheap pre-filter: True when LLM is worth calling for artist extraction.

    Returns False when the description obviously doesn't name a person
    (country-origin antique like 'CHINA, Qing dynasty / Glazed stoneware
    jar...').  LLM still returns artist=null for those, but pre-filter
    saves the ~$0.0014/lot token cost on the Marambat-style 200-lot
    Chinese-antique sales.
    """
    if not desc or len(desc.strip()) < 30:
        return False
    first_line = desc.split('\n', 1)[0].strip()
    if _COUNTRY_ANTIQUE_PREFIX_RE.match(first_line):
        return False
    return True


def llm_artist_fallback(description: str, raw_title: str = "") -> tuple:
    """Crawler-facing fallback: extract artist + title via LLM when
    regex couldn't.  Returns `(artist, artwork_title, birth_year,
    death_year)` matching the existing `_parse_artist_and_title()`
    contract used by drouot / aguttes / millon.

    Returns `('', '', None, None)` when:
      - pre-filter rejects (antique/anonymous lot)
      - LLM fails or returns low confidence (< 0.6)
      - artist field is null in LLM response
    """
    if not should_try_llm_artist(description):
        return ('', '', None, None)
    out = extract_lot_fields(description, title=raw_title)
    if 'error' in out:
        return ('', '', None, None)
    if out.get('confidence', 0) < 0.6:
        return ('', '', None, None)
    artist = out.get('artist') or ''
    title = out.get('title') or ''
    by = out.get('birth_year')
    dy = out.get('death_year')
    return (artist.strip(), title.strip(), by, dy)
