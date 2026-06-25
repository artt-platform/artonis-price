"""Provenance cleanup — strip bilingual Chinese block from Sotheby's
and other Asian-bilingual catalogs.

Sotheby's appends a Chinese translation block separated by a long
'-----' rule.  For VN art collectors browsing the EN site, the EN
portion is what matters; the ZH block is noise that breaks searching.
"""
import re
import html as _html


_SEP_RE = re.compile(r"-{10,}")
_ZH_RE = re.compile(r'[一-鿿]')
# Numeric character references like &#31680; or &#x7BC0; — Bonhams
# sometimes stores provenance with HTML-entity-encoded CJK chars.
_HTML_NUMERIC_REF_RE = re.compile(r'&#(?:x[0-9a-fA-F]+|\d+);')


def strip_bilingual(prov: str) -> str:
    """Return EN-only provenance.  Pass-through when no Chinese chars
    detected — never lossy on Latin-only input.

    Handles three forms of CJK contamination:
      1. Raw chars: 節日 lacquer ...
      2. HTML numeric refs: &#31680;&#26085; lacquer ...
      3. Long '------' rule separating EN from ZH translation.
    """
    if not prov:
        return ""
    # Decode HTML numeric refs first so the Chinese-detector sees them
    if _HTML_NUMERIC_REF_RE.search(prov):
        prov = _html.unescape(prov)
    if not _ZH_RE.search(prov):
        return prov
    parts = _SEP_RE.split(prov, maxsplit=1)
    en = parts[0].strip()
    if _ZH_RE.search(en):
        # Try newline split first (Sothebys/Aguttes catalogues)
        per_line = "\n".join(
            line for line in en.split("\n") if not _ZH_RE.search(line)
        ).strip()
        if per_line and len(per_line) >= 5:
            en = per_line
        else:
            # Bonhams: ZH appended as final sentence on one line.
            # Split on sentence ends and drop ZH-tainted ones.
            segs = re.split(r'(?<=[.!?])\s+', en)
            keep = [s.strip() for s in segs if s.strip() and not _ZH_RE.search(s)]
            en = " ".join(keep).strip()
    if not en or len(en) < 5 or _ZH_RE.search(en):
        return prov  # give up; return original rather than empty
    return en
