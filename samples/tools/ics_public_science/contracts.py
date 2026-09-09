"""Small shared value normalizers for the literature connector."""

from __future__ import annotations

import re


_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def normalize_doi(value: str) -> str:
    """Return one canonical DOI or reject a malformed represented value."""

    clean = re.sub(
        r"^(?:https?://)?(?:dx\.)?doi\.org/",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    ).lower()
    if len(clean) > 255 or not _DOI_RE.fullmatch(clean):
        raise ValueError("expected a bounded DOI beginning with '10.'")
    return clean
