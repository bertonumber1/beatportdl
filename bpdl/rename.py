"""Re-apply the current naming templates to folders that are already downloaded.

The templates in the config only ever run at download time. Change
`release_directory_template` afterwards and every folder already on disk keeps the name it
was born with, so a library ends up in two or more naming conventions with no way back
short of deleting and re-downloading it.

This walks a downloads tree, reconstructs each release's naming values from the tags bp-dl
embedded in the audio itself, re-renders the current template and renames the folder to
match. No Beatport calls, no re-download: `catalognumber`, `albumartist`, `album`, `label`
and `date` are all written into every track at download time, which is exactly the set the
release templates draw on.

    from bpdl import rename
    for row in rename.plan("/downloads/beatport", cfg):
        print(row.old, "->", row.new)
    rename.apply(rows)

Placeholders that tags cannot recover — `{upc}`, `{slug}`, `{remixers}`, `{bpm_range}` —
are reported as `missing` and those folders are LEFT ALONE rather than renamed to something
containing a literal '{upc}'. That is the honest failure: a template needing data the files
do not carry cannot be re-applied offline, and silently writing a broken name would be
worse than not renaming.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from bpdl.templates import _PLACEHOLDER_RE, parse_template, sanitize_for_path, sanitize_path

AUDIO_EXT = (".flac", ".mp3", ".m4a", ".aiff", ".aif", ".wav")

# tag name -> the spellings it goes by across FLAC vorbis comments and ID3
_TAGS = {
    "album": ("album", "TALB"),
    "albumartist": ("albumartist", "album artist", "TPE2"),
    "artist": ("artist", "TPE1"),
    "catalognumber": ("catalognumber", "catalog_number", "CATALOGNUMBER",
                      "TXXX:CATALOGNUMBER"),
    "label": ("label", "organization", "publisher", "TPUB"),
    "date": ("date", "year", "TDRC", "TYER"),
    "totaltracks": ("totaltracks", "tracktotal"),
    "beatport_release_id": ("beatport_release_id", "BEATPORT_RELEASE_ID",
                            "TXXX:BEATPORT_RELEASE_ID"),
}


@dataclass
class Row:
    old: str
    new: str
    missing: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.new) and os.path.basename(self.old) != self.new


def _first(audio, key: str) -> str:
    for name in _TAGS.get(key, (key,)):
        try:
            v = audio.get(name)
        except Exception:
            v = None
        if not v:
            continue
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        return str(v).strip()
    return ""


def read_values(folder: str, artists_limit: int = 3, artists_short_form: str = "VA") -> dict:
    """Naming values for one release folder, reconstructed from its tracks' tags.

    `albumartist` is preferred over `artist` because it is the RELEASE artist, which is
    what the release template's {artists} means — on a compilation the two differ on every
    single track. Where the tracks disagree about the album artist the release is treated
    as various, matching display_artists() hitting its limit.
    """
    files = []
    for f in sorted(os.listdir(folder)):
        if f.lower().endswith(AUDIO_EXT):
            files.append(os.path.join(folder, f))
    if not files:
        return {}

    seen_album_artists: list[str] = []
    vals: dict[str, str] = {}
    for path in files:
        # The album artist is a RELEASE-level tag, identical on every track, so the first
        # readable file normally settles the whole folder. Only keep opening files when it
        # did not — on a USB mount an open costs ~70 ms and a library is thousands of
        # folders, which is the difference between a snappy rescan and a two-minute one.
        if vals and seen_album_artists:
            break
        try:
            audio = mutagen.File(path)
        except Exception:
            continue
        if audio is None:
            continue
        aa = _first(audio, "albumartist") or _first(audio, "artist")
        if aa and aa not in seen_album_artists:
            seen_album_artists.append(aa)
        if vals:
            continue
        date = _first(audio, "date")
        vals = {
            "id": _first(audio, "beatport_release_id"),
            "name": sanitize_for_path(_first(audio, "album")),
            "catalog_number": sanitize_for_path(_first(audio, "catalognumber")),
            "label": sanitize_for_path(_first(audio, "label")),
            "date": date,
            "year": date[:4] if len(date) >= 4 else "",
            "track_count": _first(audio, "totaltracks") or str(len(files)),
        }
    if not vals:
        return {}

    vals["artists"] = sanitize_for_path(
        artists_display(seen_album_artists, artists_limit, artists_short_form))
    return {k: v for k, v in vals.items() if v != ""}


def artists_display(tag_values: list[str], limit: int = 3, short_form: str = "VA") -> str:
    """Rebuild what display_artists() would have produced, from tag values.

    display_artists() counts the release's artist LIST and collapses to "VA" past the
    limit. A tag holds that list ALREADY JOINED with ", ", so counting tag values sees one
    artist where Beatport saw ten and spells all of them out — producing a 250-character
    truncated folder name where a real download writes "VA". Splitting the join back apart
    restores the count. Names are de-duplicated in first-seen order so a compilation whose
    tracks repeat an artist does not inflate past the limit.
    """
    names: list[str] = []
    for value in tag_values:
        for part in value.split(", "):
            part = part.strip()
            if part and part not in names:
                names.append(part)
    if short_form and limit and len(names) > limit:
        return short_form
    return ", ".join(names)


def is_release_dir(path: str) -> bool:
    return any(f.lower().endswith(AUDIO_EXT) for f in os.listdir(path))


def find_release_dirs(root: str) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        if any(f.lower().endswith(AUDIO_EXT) for f in filenames):
            out.append(dirpath)
            dirnames[:] = []          # a release folder has no release folders inside it
    return sorted(out)


def plan(root: str, cfg, template: str | None = None) -> list[Row]:
    """What renaming this tree under the current template would do. Touches nothing."""
    template = template or cfg.release_directory_template
    wanted = set(_PLACEHOLDER_RE.findall(template))
    rows: list[Row] = []
    for d in find_release_dirs(root):
        vals = read_values(d, cfg.artists_limit, cfg.artists_short_form)
        if not vals:
            rows.append(Row(d, "", reason="no readable tags"))
            continue
        missing = sorted(wanted - set(vals))
        if missing:
            rows.append(Row(d, "", missing=missing,
                            reason="tags cannot supply " + ", ".join("{%s}" % m
                                                                     for m in missing)))
            continue
        new = sanitize_path(parse_template(template, vals), cfg.whitespace_character)
        rows.append(Row(d, new))
    return rows


def apply(rows: list[Row]) -> tuple[int, list[str]]:
    """Rename the folders in `rows`. Returns (renamed, problems)."""
    done, problems = 0, []
    for r in rows:
        if not r.changed:
            continue
        dest = str(Path(r.old).parent / r.new)
        if os.path.exists(dest):
            problems.append(f"{r.new}: target already exists, left alone")
            continue
        try:
            os.rename(r.old, dest)
            done += 1
        except OSError as e:
            problems.append(f"{r.new}: {e}")
    return done, problems
