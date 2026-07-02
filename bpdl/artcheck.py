from __future__ import annotations

from pathlib import Path

import requests

from bpdl.api import BeatportClient
from bpdl.config import AppConfig
from bpdl.tagging import has_embedded_art, read_embedded_ids, write_cover

AUDIO_EXTENSIONS = (".flac", ".m4a")


def find_audio_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [p for p in root.rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS]


def _fetch_cover_bytes(release_id: int, cover_size: str, bp: BeatportClient, bs: BeatportClient) -> bytes | None:
    for client in (bp, bs):
        try:
            release = client.get_release(release_id)
        except Exception:
            continue
        url = release.image.formatted_url(cover_size) if release.image.dynamic_uri else ""
        if not url:
            continue
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException:
            continue
    return None


def recheck_art(
    cfg: AppConfig,
    bp: BeatportClient,
    bs: BeatportClient,
    only_missing: bool = True,
    on_progress=None,
) -> dict:
    """Walks the downloads directory for FLAC/M4A files, groups them by the
    embedded Beatport/Beatsource release ID, and re-fetches + re-embeds cover
    art for each release (once per release, applied to every track file in it).
    Files predating the release_id tag (added to the default mapping alongside
    this feature) are reported separately since they can't be matched back."""
    root = Path(cfg.downloads_directory)
    files = find_audio_files(root)

    by_release: dict[int, list[Path]] = {}
    no_id_tag = 0
    already_ok = 0
    unreadable = 0
    for f in files:
        # The downloads directory may be shared with other tools/incomplete files —
        # one unreadable/corrupt file must not abort the whole recheck run.
        try:
            release_id, _track_id = read_embedded_ids(str(f))
        except Exception:
            unreadable += 1
            continue
        if not release_id:
            no_id_tag += 1
            continue
        try:
            has_art = has_embedded_art(str(f))
        except Exception:
            unreadable += 1
            continue
        if only_missing and has_art:
            already_ok += 1
            continue
        by_release.setdefault(release_id, []).append(f)

    total_releases = len(by_release)
    releases_fixed = 0
    files_fixed = 0
    failed = 0

    for i, (release_id, paths) in enumerate(sorted(by_release.items()), 1):
        if on_progress:
            on_progress(f"Release {i}/{total_releases} — fetching cover for release {release_id} ({len(paths)} track(s))...")
        cover_bytes = _fetch_cover_bytes(release_id, cfg.cover_size, bp, bs)
        if not cover_bytes:
            failed += len(paths)
            continue
        release_ok = False
        for f in paths:
            try:
                write_cover(str(f), cover_bytes)
                files_fixed += 1
                release_ok = True
            except Exception:
                failed += 1
        if release_ok:
            releases_fixed += 1

    return {
        "scanned_files": len(files),
        "already_ok": already_ok,
        "no_id_tag": no_id_tag,
        "unreadable": unreadable,
        "releases_checked": total_releases,
        "releases_fixed": releases_fixed,
        "files_fixed": files_fixed,
        "failed": failed,
    }
