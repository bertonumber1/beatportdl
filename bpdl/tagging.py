from __future__ import annotations

from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover

from bpdl.config import AppConfig
from bpdl.models import Track, display_artists
from bpdl.templates import number_with_padding

_M4A_FREEFORM_MEAN = "com.apple.iTunes"


def _mapping_values(track: Track, cfg: AppConfig) -> dict[str, str]:
    subgenre = track.subgenre.name if track.subgenre else ""
    release = track.release
    return {
        "track_id": str(track.id),
        "track_url": track.store_url(),
        "track_name": f"{track.name} ({track.mix_name})",
        "track_artists": display_artists(track.artists),
        "track_remixers": display_artists(track.remixers),
        "track_artists_limited": display_artists(track.artists, cfg.artists_limit, cfg.artists_short_form),
        "track_remixers_limited": display_artists(track.remixers, cfg.artists_limit, cfg.artists_short_form),
        "track_number": str(track.number),
        "track_number_with_padding": number_with_padding(track.number, release.track_count, cfg.track_number_padding),
        "track_number_with_total": f"{track.number}/{release.track_count}",
        "track_genre": track.genre.name,
        "track_subgenre": subgenre,
        "track_genre_with_subgenre": track.genre_with_subgenre("|"),
        "track_subgenre_or_genre": track.subgenre_or_genre(),
        "track_key": track.key.display(cfg.key_system),
        "track_bpm": str(track.bpm),
        "track_isrc": track.isrc,
        "release_id": str(release.id),
        "release_url": release.store_url(),
        "release_name": release.name,
        "release_artists": display_artists(release.artists),
        "release_remixers": display_artists(release.remixers),
        "release_artists_limited": display_artists(release.artists, cfg.artists_limit, cfg.artists_short_form),
        "release_remixers_limited": display_artists(release.remixers, cfg.artists_limit, cfg.artists_short_form),
        "release_date": release.date,
        "release_year": release.year(),
        "release_track_count": str(release.track_count),
        "release_track_count_with_padding": number_with_padding(
            release.track_count, release.track_count, cfg.track_number_padding
        ),
        "release_catalog_number": release.catalog_number,
        "release_upc": release.upc,
        "release_label": release.label.name,
        "release_label_url": release.label.store_url(),
    }


def tag_track(location: str, track: Track, cover_path: str | None, cfg: AppConfig) -> None:
    if not cfg.fix_tags:
        return

    ext = Path(location).suffix.lower()
    values = _mapping_values(track, cfg)
    cover_data = None
    if cover_path and (cfg.cover_size != "1400x1400" or ext == ".m4a"):
        cover_data = Path(cover_path).read_bytes()

    if ext == ".flac":
        _tag_flac(location, values, cover_data, cfg.tag_mappings.get("flac", {}))
    elif ext == ".m4a":
        _tag_m4a(location, values, cover_data, cfg.tag_mappings.get("m4a", {}))
    else:
        raise ValueError(f"unsupported file extension for tagging: {ext}")


def _tag_flac(location: str, values: dict[str, str], cover_data: bytes | None, mapping: dict[str, str]) -> None:
    audio = FLAC(location)
    audio.clear()

    for field, tag in mapping.items():
        value = values.get(field, "")
        if value:
            audio[tag] = value

    # Only touch embedded pictures when we actually fetched a replacement — Beatport's
    # FLAC stream already ships its own cover art, and clearing pictures unconditionally
    # (regardless of whether cover_data is set) would silently strip it with nothing to
    # replace it under default settings (lossless quality, default cover size).
    if cover_data:
        audio.clear_pictures()
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = cover_data
        audio.add_picture(pic)

    audio.save()


# MP4 has no arbitrary-tag-name mechanism like FLAC's Vorbis comments — each
# property must map to a real atom, or ride as an iTunes freeform "----" atom.
_M4A_STANDARD_ATOMS = {
    "TITLE": "\xa9nam",
    "ARTIST": "\xa9ART",
    "ALBUM": "\xa9alb",
    "ALBUMARTIST": "aART",
    "DATE": "\xa9day",
    "GENRE": "\xa9gen",
}


def _tag_m4a(location: str, values: dict[str, str], cover_data: bytes | None, mapping: dict[str, str]) -> None:
    audio = MP4(location)
    audio.clear()

    track_number, total_tracks = None, None
    for field, tag in mapping.items():
        value = values.get(field, "")
        if not value:
            continue
        if tag == "TRACKNUMBER":
            track_number = value
        elif tag == "TOTALTRACKS":
            total_tracks = value
        elif tag == "BPM":
            audio["tmpo"] = [int(value)]
        elif tag in _M4A_STANDARD_ATOMS:
            audio[_M4A_STANDARD_ATOMS[tag]] = [value]
        else:
            audio[f"----:{_M4A_FREEFORM_MEAN}:{tag}"] = [value.encode("utf-8")]

    if track_number:
        audio["trkn"] = [(int(track_number), int(total_tracks) if total_tracks else 0)]

    if cover_data:
        audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save()


def has_embedded_art(location: str) -> bool:
    ext = Path(location).suffix.lower()
    try:
        if ext == ".flac":
            return bool(FLAC(location).pictures)
        if ext == ".m4a":
            return bool(MP4(location).get("covr"))
    except Exception:
        pass
    return False


def read_embedded_ids(location: str) -> tuple[int, int]:
    """Returns (release_id, track_id) from the BEATPORT_RELEASE_ID/BEATPORT_TRACK_ID
    tags written by tag_track(); (0, 0) if missing/unreadable (e.g. files downloaded
    before these tags were added to the default mapping)."""
    ext = Path(location).suffix.lower()
    try:
        if ext == ".flac":
            audio = FLAC(location)
            release_id = int((audio.get("BEATPORT_RELEASE_ID") or ["0"])[0] or 0)
            track_id = int((audio.get("BEATPORT_TRACK_ID") or ["0"])[0] or 0)
            return release_id, track_id
        if ext == ".m4a":
            audio = MP4(location)
            release_raw = audio.get(f"----:{_M4A_FREEFORM_MEAN}:BEATPORT_RELEASE_ID")
            track_raw = audio.get(f"----:{_M4A_FREEFORM_MEAN}:BEATPORT_TRACK_ID")
            release_id = int(bytes(release_raw[0]).decode("utf-8")) if release_raw else 0
            track_id = int(bytes(track_raw[0]).decode("utf-8")) if track_raw else 0
            return release_id, track_id
    except Exception:
        # Best-effort: any unreadable/corrupt/non-audio file (the downloads directory
        # may be shared with other tools) just means "no ID available", not a crash.
        pass
    return 0, 0


def write_cover(location: str, cover_data: bytes) -> None:
    """Replaces just the embedded cover picture, leaving every other tag alone —
    used by the art-recheck tool to repair missing/broken artwork without a full
    re-download or re-tag."""
    ext = Path(location).suffix.lower()
    if ext == ".flac":
        audio = FLAC(location)
        audio.clear_pictures()
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = cover_data
        audio.add_picture(pic)
        audio.save()
    elif ext == ".m4a":
        audio = MP4(location)
        audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
    else:
        raise ValueError(f"unsupported file extension for tagging: {ext}")
