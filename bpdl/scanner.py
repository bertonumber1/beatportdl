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


class IncompletePagination(RuntimeError):
    """A walk ran out of pages before it had seen everything the server said was
    there. Raised rather than returned, because a short walk that looks like a
    finished one is precisely how a label's catalogue silently half-downloads."""


def for_paginated(entity_id: int, params: str, fetch_page, process_item, should_stop=None) -> None:
    """should_stop, if given, is checked before every page fetch — lets a long
    walk be interrupted (e.g. on an explicit user Stop) instead of running to
    natural completion or, in a worst case, forever (see sanitize_params: a
    pasted URL with its own page=/per_page= query params can make the server
    always re-serve the same page, so paginated.next never goes falsy).

    The end of pagination is believed only once the number of items actually
    walked reaches the total the server itself reported. `next` alone is not
    evidence of completion: one truncated response ends the walk
    indistinguishably from a real finish, and the caller then treats a fraction
    of a catalogue as the whole of it.

    A short finish re-fetches the same page once before giving up. Only the items
    that page did not serve the first time are processed, so the retry cannot
    double-count — which matters because process_item has side effects (it queues
    downloads and writes baseline rows). Still short after the retry is an error,
    not a result.
    """
    page = 1
    seen = 0
    done_on_page = 0   # items of `page` already handed to process_item
    retried = False
    while True:
        if should_stop and should_stop():
            return
        paginated = fetch_page(entity_id, page, params)
        fresh = paginated.results[done_on_page:]
        for i, item in enumerate(fresh, start=done_on_page):
            process_item(item, i)
        seen += len(fresh)
        done_on_page += len(fresh)

        if paginated.next:
            page += 1
            done_on_page = 0
            retried = False
            continue

        # count is absent (0) on endpoints that don't report a total — nothing to
        # check against there, so the old behaviour stands.
        total = paginated.count or 0
        if total and seen < total:
            if not retried:
                retried = True
                continue
            raise IncompletePagination(
                f"pagination ended at page {page} after {seen} of {total} items")
        return


def sanitize_params(params: str) -> str:
    """Strips page/per_page from a URL's raw query string before it's forwarded
    into our own paginated requests. link.params comes straight from whatever
    query string a pasted URL happened to have — if that includes page=/
    per_page= (e.g. copy-pasted from a scrolled Beatport catalogue page), it
    collides with our own page={page} in the constructed request URL. Django
    QueryDict.get() resolves a repeated key to the LAST occurrence, so that
    stale page= value silently wins over ours every time — pagination gets
    stuck re-serving the same page forever, and paginated.next never goes
    falsy because the server thinks it already served the "current" page."""
    if not params:
        return params
    from urllib.parse import parse_qsl, urlencode

    pairs = parse_qsl(params, keep_blank_values=True)
    filtered = [(k, v) for k, v in pairs if k not in ("page", "per_page")]
    return urlencode(filtered)


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

    for_paginated(link.id, sanitize_params(link.params), client.get_label_releases, on_release)
    return stats


def scan_artist(client: BeatportClient, link: Link, on_progress=None) -> ScanStats:
    stats = ScanStats()

    def on_track(track, _i):
        stats.add(track)
        if on_progress:
            on_progress(f"{stats.total} tracks scanned...")

    for_paginated(link.id, sanitize_params(link.params), client.get_artist_tracks, on_track)
    return stats
