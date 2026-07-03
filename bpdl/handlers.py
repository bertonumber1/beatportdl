from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.console import Console

from bpdl import history
from bpdl.api import BeatportClient
from bpdl.config import AppConfig
from bpdl.download import (
    RunStats,
    download_cover,
    handle_cover_file,
    handle_track,
    require_cover,
    skippable_reason,
    track_matches_filter,
)
from bpdl.links import ARTIST_LINK, CHART_LINK, LABEL_LINK, PLAYLIST_LINK, RELEASE_LINK, TRACK_LINK, Link, parse_url
from bpdl.models import NamingPreferences
from bpdl.scanner import for_paginated

console = Console()


class App:
    def __init__(self, cfg: AppConfig, bp: BeatportClient, bs: BeatportClient, on_event=None):
        self.cfg = cfg
        self.bp = bp
        self.bs = bs
        self.stats = RunStats()
        self.active_files: set[str] = set()
        self.active_files_lock = threading.Lock()
        self.global_pool = ThreadPoolExecutor(max_workers=cfg.max_global_workers)
        self.download_pool = ThreadPoolExecutor(max_workers=cfg.max_download_workers)
        # Optional sink for structured progress events (used by the web UI's SSE
        # stream). None in normal CLI/TUI use, where rich console.print is enough.
        self.on_event = on_event
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        """Cooperative cancellation: stops new work from being submitted/started.
        Tracks already mid-download will still finish that single file (no hard
        kill mid-write), but nothing further will start."""
        self.cancelled.set()

    def shutdown(self, cancel_pending: bool = False) -> None:
        """Waits for all submitted work to finish. cancel_pending=True additionally
        drops anything still queued but not yet started — only appropriate right
        after an explicit user Stop; the default (False) is required for a normal
        completion, since a label/artist submits far more release/track tasks than
        there are worker threads, and pending (not-yet-started) tasks are exactly
        the bulk of a large catalogue's work, not leftover cruft to discard."""
        self.global_pool.shutdown(wait=True, cancel_futures=cancel_pending)
        self.download_pool.shutdown(wait=True, cancel_futures=cancel_pending)

    def _log_error(self, url: str, step: str, err: Exception) -> None:
        console.print(f"[red][{url}][/red] {step}: {err}")
        self.stats.add_failed()

    def _skip_or_error(self, url: str, step: str, err: Exception) -> None:
        reason = skippable_reason(err)
        if reason:
            console.print(f"[yellow][{url}][/yellow] skipped ({reason})")
            self.stats.add_skipped(reason)
        else:
            self._log_error(url, step, err)

    def _setup_downloads_dir(self, base_dir: str, entity, kind: str) -> str:
        if self.cfg.sort_by_context:
            template_map = {
                "release": self.cfg.release_directory_template,
                "playlist": self.cfg.playlist_directory_template,
                "chart": self.cfg.chart_directory_template,
                "label": self.cfg.label_directory_template,
                "artist": self.cfg.artist_directory_template,
            }
            naming = NamingPreferences(
                template=template_map[kind],
                whitespace=self.cfg.whitespace_character,
                artists_limit=self.cfg.artists_limit,
                artists_short_form=self.cfg.artists_short_form,
                track_number_padding=self.cfg.track_number_padding,
            )
            sub_dir = entity.directory_name(naming)
            if kind == "release" and self.cfg.sort_by_label:
                base_dir = str(Path(base_dir) / entity.label.name)
            base_dir = str(Path(base_dir) / sub_dir)
        Path(base_dir).mkdir(parents=True, exist_ok=True)
        return base_dir

    def _cleanup(self, downloads_dir: str) -> None:
        if downloads_dir != self.cfg.downloads_directory:
            try:
                Path(downloads_dir).rmdir()
            except OSError:
                pass

    def _handle_track(self, client: BeatportClient, track, downloads_dir: str, cover: str | None) -> None:
        if self.cancelled.is_set():
            return
        track_key = str(track.id)
        subgenre = track.subgenre.name if track.subgenre else ""
        artists_str = ", ".join(a.get("name", "") for a in track.artists)

        if self.cfg.skip_previously_downloaded and history.is_track_downloaded(track.id):
            if self.on_event:
                self.on_event({"type": "track_skipped", "id": track_key, "reason": "already downloaded", "url": track.store_url()})
            self.stats.add_skipped("already downloaded")
            return

        if self.on_event:
            self.on_event({
                "type": "track_start",
                "id": track_key,
                "name": track.name,
                "mix_name": track.mix_name,
                "artists": [a.get("name", "") for a in track.artists],
                "release": track.release.name,
                "cover": track.release.image.formatted_url("300x300") if track.release.image.dynamic_uri else "",
                "url": track.store_url(),
            })

        def _progress(downloaded: int, total: int) -> None:
            if self.on_event:
                self.on_event({"type": "track_progress", "id": track_key, "downloaded": downloaded, "total": total})

        def _record(status: str, location: str = "", reason: str = "") -> None:
            try:
                size_bytes = 0
                if location:
                    try:
                        size_bytes = Path(location).stat().st_size
                    except OSError:
                        pass
                history.record(
                    track_id=track.id, release_id=track.release.id, store=track.store,
                    track_name=track.name, artists=artists_str, release_name=track.release.name,
                    label=track.release.label.name, genre=track.genre.name, subgenre=subgenre,
                    bpm=track.bpm, key=track.key.display(self.cfg.key_system), quality=self.cfg.quality,
                    file_path=location, file_size_bytes=size_bytes, status=status, reason=reason,
                )
            except Exception:
                pass  # history is best-effort — never let it break a real download

        try:
            location = handle_track(
                client, track, downloads_dir, cover, self.cfg, self.active_files, self.active_files_lock,
                on_progress=_progress,
            )
            if location:
                self.stats.add_downloaded()
                _record(history.STATUS_DOWNLOADED, location=location)
                if self.on_event:
                    self.on_event({"type": "track_done", "id": track_key, "location": location})
            else:
                # handle_track() returns None when save_track() found the destination
                # file already on disk and track_exists=="skip" — nothing was actually
                # downloaded or tagged, so this must never be recorded as a download.
                self.stats.add_skipped("file exists")
                _record(history.STATUS_SKIPPED, reason="file already exists (track_exists=skip)")
                if self.on_event:
                    self.on_event({"type": "track_skipped", "id": track_key, "reason": "file already exists", "url": track.store_url()})
        except Exception as e:
            reason = skippable_reason(e)
            _record(history.STATUS_SKIPPED if reason else history.STATUS_FAILED, reason=reason or str(e))
            if self.on_event:
                self.on_event({
                    "type": "track_skipped" if reason else "track_error",
                    "id": track_key,
                    "name": f"{track.name} ({track.mix_name})" if track.mix_name else track.name,
                    "reason": reason or str(e),
                    "url": track.store_url(),
                })
            self._skip_or_error(track.store_url(), "handle track", e)

    def handle_url(self, url: str) -> None:
        try:
            link = parse_url(url)
        except Exception as e:
            self._log_error(url, "parse url", e)
            return

        client = self.bs if link.store == "beatsource" else self.bp
        dispatch = {
            TRACK_LINK: self._handle_track_link,
            RELEASE_LINK: self._handle_release_link,
            PLAYLIST_LINK: self._handle_playlist_link,
            CHART_LINK: self._handle_chart_link,
            LABEL_LINK: self._handle_label_link,
            ARTIST_LINK: self._handle_artist_link,
        }
        fn = dispatch.get(link.type)
        if not fn:
            self._log_error(url, "handle url", ValueError(f"unsupported link type: {link.type}"))
            return
        fn(client, link)

    def _handle_track_link(self, client: BeatportClient, link: Link) -> None:
        try:
            track = client.get_track(link.id)
            release = client.get_release(track.release.id)
            track.release = release
        except Exception as e:
            self._log_error(link.original, "fetch track", e)
            return

        downloads_dir = self._setup_downloads_dir(self.cfg.downloads_directory, release, "release")
        cover = None
        if require_cover(self.cfg, True, True):
            try:
                cover = download_cover(release.image.formatted_url(self.cfg.cover_size), downloads_dir)
            except Exception as e:
                self._log_error(link.original, "download cover", e)

        self._handle_track(client, track, downloads_dir, cover)
        handle_cover_file(cover or "", self.cfg)
        self._cleanup(downloads_dir)

    def _handle_release_link(self, client: BeatportClient, link: Link) -> None:
        try:
            release = client.get_release(link.id)
        except Exception as e:
            self._log_error(link.original, "fetch release", e)
            return

        downloads_dir = self._setup_downloads_dir(self.cfg.downloads_directory, release, "release")
        cover = None
        if require_cover(self.cfg, True, True):
            try:
                cover = download_cover(release.image.formatted_url(self.cfg.cover_size), downloads_dir)
            except Exception as e:
                self._log_error(link.original, "download cover", e)

        futures = []
        for track_url in release.track_urls:
            futures.append(self.download_pool.submit(self._download_release_track, client, track_url, release, downloads_dir, cover))
        for f in futures:
            f.result()

        handle_cover_file(cover or "", self.cfg)
        self._cleanup(downloads_dir)

    def _download_release_track(self, client, track_url, release, downloads_dir, cover) -> None:
        try:
            track_link = parse_url(track_url)
            track = client.get_track(track_link.id)
            track.release = release
        except Exception as e:
            self._log_error(track_url, "fetch release track", e)
            return
        self._handle_track(client, track, downloads_dir, cover)

    def _handle_label_link(self, client: BeatportClient, link: Link) -> None:
        try:
            label = client.get_label(link.id)
        except Exception as e:
            self._log_error(link.original, "fetch label", e)
            return

        downloads_dir = self._setup_downloads_dir(self.cfg.downloads_directory, label, "label")

        def on_release(release, _i):
            if self.cancelled.is_set():
                return
            self.global_pool.submit(self._handle_label_release, client, release, downloads_dir)

        try:
            for_paginated(link.id, link.params, client.get_label_releases, on_release)
        except Exception as e:
            self._log_error(link.original, "handle label releases", e)

    def _handle_label_release(self, client: BeatportClient, release, downloads_dir: str) -> None:
        if self.cancelled.is_set():
            return
        release_dir = self._setup_downloads_dir(downloads_dir, release, "release")
        cover = None
        if require_cover(self.cfg, True, True):
            try:
                cover = download_cover(release.image.formatted_url(self.cfg.cover_size), release_dir)
            except Exception as e:
                self._log_error(release.store_url(), "download cover", e)

        futures = []

        def on_track(track, _i):
            if self.cancelled.is_set():
                return
            if not track_matches_filter(track, self.cfg):
                console.print(f"[yellow][{track.store_url()}][/yellow] skipped (filter)")
                self.stats.add_skipped("filter")
                return
            futures.append(self.download_pool.submit(self._fetch_and_handle_track, client, track, release, release_dir, cover))

        try:
            for_paginated(release.id, "", client.get_release_tracks, on_track)
        except Exception as e:
            self._log_error(release.store_url(), "handle release tracks", e)
            return

        for f in futures:
            f.result()

        self._cleanup(release_dir)
        handle_cover_file(cover or "", self.cfg)

    def _fetch_and_handle_track(self, client, track, release, directory, cover) -> None:
        try:
            full = client.get_track(track.id)
            full.release = release
        except Exception as e:
            self._skip_or_error(track.store_url(), "fetch full track", e)
            return
        self._handle_track(client, full, directory, cover)

    def _handle_artist_link(self, client: BeatportClient, link: Link) -> None:
        try:
            artist = client.get_artist(link.id)
        except Exception as e:
            self._log_error(link.original, "fetch artist", e)
            return

        downloads_dir = self._setup_downloads_dir(self.cfg.downloads_directory, artist, "artist")
        futures = []

        def on_track(track, _i):
            if self.cancelled.is_set():
                return
            if not track_matches_filter(track, self.cfg):
                console.print(f"[yellow][{track.store_url()}][/yellow] skipped (filter)")
                self.stats.add_skipped("filter")
                return
            futures.append(self.download_pool.submit(self._handle_artist_track, client, track, downloads_dir))

        try:
            for_paginated(link.id, link.params, client.get_artist_tracks, on_track)
        except Exception as e:
            self._log_error(link.original, "handle artist tracks", e)
            return

        for f in futures:
            f.result()

    def _handle_artist_track(self, client, track, downloads_dir) -> None:
        try:
            full = client.get_track(track.id)
            release = client.get_release(full.release.id)
            full.release = release
        except Exception as e:
            self._skip_or_error(track.store_url(), "fetch release", e)
            return

        release_dir = self._setup_downloads_dir(downloads_dir, release, "release")
        cover = None
        if require_cover(self.cfg, True, True):
            try:
                cover = download_cover(release.image.formatted_url(self.cfg.cover_size), release_dir)
            except Exception as e:
                self._log_error(track.store_url(), "download cover", e)

        self._handle_track(client, full, release_dir, cover)
        handle_cover_file(cover or "", self.cfg)
        self._cleanup(release_dir)

    def _handle_playlist_link(self, client: BeatportClient, link: Link) -> None:
        try:
            playlist = client.get_playlist(link.id)
        except Exception as e:
            self._log_error(link.original, "fetch playlist", e)
            return

        downloads_dir = self._setup_downloads_dir(self.cfg.downloads_directory, playlist, "playlist")
        futures = []

        def on_item(item, _i):
            if self.cancelled.is_set():
                return
            futures.append(self.download_pool.submit(self._handle_playlist_item, client, item, downloads_dir))

        try:
            for_paginated(link.id, "", client.get_playlist_items, on_item)
        except Exception as e:
            self._log_error(link.original, "handle playlist items", e)
            return

        for f in futures:
            f.result()

    def _handle_playlist_item(self, client, item, downloads_dir) -> None:
        track = item["track"]
        try:
            release = client.get_release(track.release.id)
            track.release = release
            full = client.get_track(track.id)
            track.number = full.number
        except Exception as e:
            self._skip_or_error(track.store_url(), "fetch track release", e)
            return

        track_dir = downloads_dir
        if self.cfg.sort_by_context and self.cfg.force_release_directories:
            track_dir = self._setup_downloads_dir(downloads_dir, release, "release")

        cover = None
        if require_cover(self.cfg, True, self.cfg.force_release_directories):
            try:
                cover = download_cover(release.image.formatted_url(self.cfg.cover_size), track_dir)
            except Exception as e:
                self._log_error(track.store_url(), "download cover", e)

        self._handle_track(client, track, track_dir, cover)
        if self.cfg.force_release_directories:
            handle_cover_file(cover or "", self.cfg)
        self._cleanup(track_dir)

    def _handle_chart_link(self, client: BeatportClient, link: Link) -> None:
        try:
            chart = client.get_chart(link.id)
        except Exception as e:
            self._log_error(link.original, "fetch chart", e)
            return

        downloads_dir = self._setup_downloads_dir(self.cfg.downloads_directory, chart, "chart")
        if require_cover(self.cfg, False, True):
            try:
                cover = download_cover(chart.image.formatted_url(self.cfg.cover_size), downloads_dir)
                handle_cover_file(cover, self.cfg)
            except Exception as e:
                self._log_error(link.original, "download chart cover", e)

        futures = []

        def on_track(track, _i):
            if self.cancelled.is_set():
                return
            futures.append(self.download_pool.submit(self._handle_playlist_item, client, {"track": track}, downloads_dir))

        try:
            for_paginated(link.id, "", client.get_chart_tracks, on_track)
        except Exception as e:
            self._log_error(link.original, "handle chart tracks", e)
            return

        for f in futures:
            f.result()
