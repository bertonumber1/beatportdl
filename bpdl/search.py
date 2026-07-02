from __future__ import annotations

import re

_STORE_TAG_RE = re.compile(r"@(\w+)")


def extract_store_tag(query: str) -> tuple[str, str]:
    match = _STORE_TAG_RE.search(query)
    if not match:
        return "", query
    store = match.group(1)
    trimmed = _STORE_TAG_RE.sub("", query).strip()
    return store, trimmed
