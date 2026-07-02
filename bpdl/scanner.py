from __future__ import annotations

from dataclasses import dataclass, field

from bpdl.api import BeatportClient
from bpdl.download import skippable_reason
from bpdl.links import Link
from bpdl.models import Track


@dataclass
class RankEntry:
    name: str
    count: int


@dataclass
class ScanStats:
    genres: dict[str, int] = field(default_factory=dict)
    subgenres: dict[str, int] = field(default_factory=dict)
    artists: dict[str, int] = field(default_factory=dict)
    bpm_min: int = 9999
    bpm_max: int = 0
    total: int = 0

    def add(self, track: Track) -> None:
        self.total += 1
        self.genres[track.genre.name] = self.genres.get(track.genre.name, 0) + 1
        if track.subgenre and track.subgenre.name:
            self.subgenres[track.subgenre.name] = self.subgenres.get(track.subgenre.name, 0) + 1
        for a in track.artists:
            self.artists[a["name"]] = self.artists.get(a["name"], 0) + 1
        if track.bpm > 0:
            self.bpm_min = min(self.bpm_min, track.bpm)
            self.bpm_max = max(self.bpm_max, track.bpm)


def rank_map(m: dict[str, int]) -> list[RankEntry]:
    return sorted((RankEntry(k, v) for k, v in m.items()), key=lambda e: (-e.count, e.name))


def for_paginated(entity_id: int, params: str, fetch_page, process_item) -> None:
    page = 1
    while True:
        paginated = fetch_page(entity_id, page, params)
        for i, item in enumerate(paginated.results):
            process_item(item, i)
        if not paginated.next:
            break
        page += 1


def scan_label(client: BeatportClient, link: Link, on_progress=None) -> ScanStats:
    """on_progress(str), if given, is called with a live status line as releases
    are scanned — the caller decides how to display it (print, TUI status bar, ...)."""
    stats = ScanStats()
    release_count = 0

    def on_release(release, _i):
        nonlocal release_count
        release_count += 1
        if on_progress:
            on_progress(f"Scanning release {release_count} — {stats.total} tracks found so far...")

        def on_track(track, _j):
            stats.add(track)

        try:
            for_paginated(release.id, "", client.get_release_tracks, on_track)
        except Exception as e:
            # Territory-restricted/pre-release/unavailable releases are expected —
            # skip this one and keep scanning the rest of the label's catalogue.
            if not skippable_reason(e):
                raise

    for_paginated(link.id, link.params, client.get_label_releases, on_release)
    return stats


def scan_artist(client: BeatportClient, link: Link, on_progress=None) -> ScanStats:
    stats = ScanStats()

    def on_track(track, _i):
        stats.add(track)
        if on_progress:
            on_progress(f"{stats.total} tracks scanned...")

    for_paginated(link.id, link.params, client.get_artist_tracks, on_track)
    return stats
