"""The web UI's translations must be complete and consistent.

A missing key is invisible until a Spanish or Dutch user hits that one screen, so
these checks run over the actual asset files rather than trusting review:

  * every `data-i18n*` key in index.html exists in the English dictionary
  * every `t("...")` key in app.js exists in the English dictionary
  * es and nl define exactly the same key set as en — no gaps, no strays
  * every `{placeholder}` in an English string is present in its translations,
    so an interpolated value can never silently vanish from a message
  * no user-visible English text is left hardcoded in the markup
"""
import json
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "bpdl" / "webui" / "static"
I18N_JS = (STATIC / "i18n.js").read_text()
APP_JS = (STATIC / "app.js").read_text()
INDEX = (STATIC / "index.html").read_text()


def _dicts():
    """Pull the I18N object out of i18n.js without a JS engine.

    The literal is plain JSON apart from the trailing commas and unquoted keys we
    do not use, so slicing it out and letting json parse it keeps this test free
    of a node dependency while still reading the real shipped file.
    """
    start = I18N_JS.index("const I18N = {") + len("const I18N = ")
    depth, i = 0, start
    while True:
        if I18N_JS[i] == "{":
            depth += 1
        elif I18N_JS[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = I18N_JS[start:i + 1]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)   # block comments
    body = re.sub(r"(?m)^\s*//.*$", "", body)           # line comments
    body = re.sub(r"(?m)^(\s*)([A-Za-z_]\w*):", r'\1"\2":', body)  # bare lang keys
    body = re.sub(r",(\s*[}\]])", r"\1", body)          # trailing commas
    return json.loads(body)


LANGS = _dicts()


def test_expected_languages_present():
    assert set(LANGS) == {"en", "es", "nl"}


@pytest.mark.parametrize("lang", ["es", "nl"])
def test_translation_key_sets_match_english(lang):
    en, other = set(LANGS["en"]), set(LANGS[lang])
    assert not (en - other), f"{lang} is missing: {sorted(en - other)}"
    assert not (other - en), f"{lang} has keys English does not: {sorted(other - en)}"


@pytest.mark.parametrize("lang", ["es", "nl"])
def test_placeholders_survive_translation(lang):
    ph = lambda s: set(re.findall(r"\{(\w+)\}", s))
    bad = {k: (ph(LANGS["en"][k]), ph(LANGS[lang][k]))
           for k in LANGS["en"]
           if ph(LANGS["en"][k]) != ph(LANGS[lang].get(k, ""))}
    assert not bad, f"{lang} placeholder mismatch: {bad}"


@pytest.mark.parametrize("lang", ["es", "nl"])
def test_no_untranslated_values(lang):
    """Catch a key copied over but never actually translated."""
    same = [k for k, v in LANGS[lang].items()
            if v == LANGS["en"][k] and k not in ALLOWED_IDENTICAL]
    assert not same, f"{lang} still shows the English string for: {same}"


# Proper nouns, notation systems and format names that are the same word in all
# three languages — translating them would be wrong, not thorough.
ALLOWED_IDENTICAL = {
    "hero.tagline", "explore.top100", "settings.key_openkey", "settings.key_camelot",
    "quality.lossless", "explore.genre", "filter.genre", "wizard.genres",
    "wizard.subgenres", "stats.tile_tracks", "stats.tile_releases", "stats.tile_labels",
    "watchsec.labels", "watchsec.noun_track", "watchsec.noun_tracks",
    "watchsec.noun_prerelease", "watchsec.noun_prereleases", "settings.key_standard",
    "explore.new_releases", "explore.new_tracks", "lang.name", "explore.n_tracks",
}

# Text that is deliberately never translated.
BRAND = {"Unspok3n", "Smash-n-Grab remix", "BP-DL"}


def test_html_keys_all_defined():
    keys = set(re.findall(r'data-i18n(?:-placeholder|-title|-html)?="([^"]+)"', INDEX))
    missing = sorted(k for k in keys if k not in LANGS["en"])
    assert not missing, f"index.html references undefined keys: {missing}"
    assert len(keys) > 100, "suspiciously few translated elements in the markup"


def test_app_js_keys_all_defined():
    keys = set(re.findall(r'\bt\("([a-z_]+\.[a-z0-9_]+)"', APP_JS))
    missing = sorted(k for k in keys if k not in LANGS["en"])
    assert not missing, f"app.js references undefined keys: {missing}"
    assert len(keys) > 80, "suspiciously few t() calls in app.js"


def test_translation_function_is_never_shadowed():
    """`t` was a track object in three loops; inside them t("key") would throw."""
    shadow = re.findall(r"(?:const|let|var)\s+t\s*[=;)]|\(\s*t\s*[,)]|for\s*\(\s*const\s+t\s+of", APP_JS)
    assert not shadow, f"a local named `t` shadows the translator: {shadow}"


def test_no_hardcoded_english_left_in_markup():
    """Any element with visible text must carry a key (or hold only markup/entities)."""
    stripped = re.sub(r"<!--.*?-->", "", INDEX, flags=re.S)
    stripped = re.sub(r"<(script|style|svg)\b.*?</\1>", "", stripped, flags=re.S)
    offenders = []
    for tag, attrs, text in re.findall(
        r"<(h1|h2|h3|p|button|label|option|small)\b([^>]*)>(.*?)</\1>", stripped, flags=re.S
    ):
        # A key on the element itself, or on a child that carries the words —
        # `<h2><span data-i18n="queue.title">Queue</span> <span id=…>0</span></h2>`
        # is fully translated even though the <h2> has no attribute of its own.
        if "data-i18n" in attrs or "data-i18n" in text:
            continue
        visible = re.sub(r"<[^>]+>", "", text)
        visible = re.sub(r"&[a-z]+;|&#\d+;", "", visible).strip()
        visible = re.sub(r"\s+", " ", visible)
        if not visible or not re.search(r"[A-Za-z]{3}", visible):
            continue
        if visible in BRAND:
            continue
        offenders.append(f"<{tag}> {visible[:60]}")
    assert not offenders, "untranslated markup: " + "; ".join(offenders)
