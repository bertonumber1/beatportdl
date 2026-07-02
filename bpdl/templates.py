import re

_PLACEHOLDER_RE = re.compile(r"\{(\w+)}")
_SANITIZE_STRING_MAP = {"\\n": "", "\\r": "", "\\t": ""}
_SANITIZE_FOR_PATH_MAP = {"\\": "", "/": ""}
_SANITIZE_PATH_CHARS = '<>:"|?*'


def sanitize_string(s: str) -> str:
    for old, new in _SANITIZE_STRING_MAP.items():
        s = s.replace(old, new)
    return " ".join(s.split())


def sanitize_for_path(s: str) -> str:
    for old, new in _SANITIZE_FOR_PATH_MAP.items():
        s = s.replace(old, new)
    return " ".join(s.split())


def sanitize_path(name: str, whitespace: str = "") -> str:
    if len(name) > 250:
        name = name[:250]
    for ch in _SANITIZE_PATH_CHARS:
        name = name.replace(ch, "")
    if whitespace:
        name = name.replace(" ", whitespace)
    return " ".join(name.split())


def number_with_padding(value: int, total: int, padding: int) -> str:
    if not padding:
        padding = len(str(total))
    return str(value).zfill(padding)


def parse_template(template: str, values: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        return values.get(key, m.group(0))

    return _PLACEHOLDER_RE.sub(repl, template)


def duration_display(length_ms: int) -> str:
    seconds = length_ms // 1000
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    remaining_seconds = seconds % 60
    if hours > 0:
        return f"{hours:02d}-{minutes:02d}-{remaining_seconds:02d}"
    return f"{minutes:02d}-{remaining_seconds:02d}"
