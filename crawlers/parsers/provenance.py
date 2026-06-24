"""Provenance cleanup — strip bilingual Chinese block from Sotheby's
and other Asian-bilingual catalogs.

Sotheby's appends a Chinese translation block separated by a long
'-----' rule.  For VN art collectors browsing the EN site, the EN
portion is what matters; the ZH block is noise that breaks searching.
"""
import re


_SEP_RE = re.compile(r"-{10,}")
_ZH_RE = re.compile(r'[一-鿿]')


def strip_bilingual(prov: str) -> str:
    """Return EN-only provenance.  Pass-through when no Chinese chars
    detected — never lossy on Latin-only input.
    """
    if not prov or not _ZH_RE.search(prov):
        return prov or ""
    parts = _SEP_RE.split(prov, maxsplit=1)
    en = parts[0].strip()
    if _ZH_RE.search(en):
        # Drop any line that still contains Chinese chars
        en = "\n".join(
            line for line in en.split("\n") if not _ZH_RE.search(line)
        ).strip()
    if not en or len(en) < 5 or _ZH_RE.search(en):
        return prov  # give up; return original rather than empty
    return en
