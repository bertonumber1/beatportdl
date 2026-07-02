from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path

import requests
from rich.console import Console

from bpdl.api import BeatportClient
from bpdl.config import AppConfig
from bpdl.models import NamingPreferences, Track
from bpdl.tagging import tag_track

console = Console()

_SKIP_PATTERNS = (
    (re.compile(r"territory|not available in your|status code: 451", re.I), "territory restricted"),
    (
        re.compile(
            r"pre-release|pre release|not yet available|not available for download|release date", re.I
        ),
        "pre-release",
    ),
    (re.compile(r"status code: 40[34]", re.I), "unavailable"),
)


def skippable_reason(err: Exception | None) -> str:
    if err is None:
        return ""
    msg = str(err)
    for pattern, reason in _SKIP_PATTERNS:
        if pattern.search(msg):
            return reason
    return ""


class TrackFileExistsError(RuntimeError):
    pass


class RunStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.downloaded = 0
        self.skipped: dict[str, int] = {}
        self.failed = 0

    def add_downloaded(self) -> None:
        with self.lock:
            self.downloaded += 1

    def add_skipped(self, reason: str) -> None:
        with self.lock:
            self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def add_failed(self) -> None:
        with self.lock:
            self.failed += 1

    def print_summary(self) -> None:
        total_skipped = sum(self.skipped.values())
        console.print(
            f"\n[bold]Run summary:[/bold] "
            f"[green]{self.downloaded} downloaded[/green], "
            f"[yellow]{total_skipped} skipped[/yellow]"
            + (f" ({', '.join(f'{v} {k}' for k, v in self.skipped.items())})" if self.skipped else "")
            + f", [red]{self.failed} failed[/red]"
        )


def _retry_get(url: str, timeout: int = 40, attempts: int = 4) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"bad status: {resp.status_code}")
        except requests.RequestException as e:
            last_exc = e
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
    raise RuntimeError(f"download failed after {attempts} attempts: {last_exc}")


def download_file(url: str, destination: str, on_progress=None) -> None:
    """Atomic download: streams to a .part file, then renames on success —
    an interrupted download never leaves a corrupt file at the final path."""
    tmp_path = destination + ".part"
    resp = _retry_get(url)
    total = int(resp.headers.get("Content-Length") or 0)
    downloaded = 0
    try:
        with open(tmp_path, "wb") as out:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                out.write(chunk)
                downloaded += len(chunk)
                if on_progress:
                    on_progress(downloaded, total)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    Path(tmp_path).rename(destination)


def download_cover(image_url: str, downloads_dir: str) -> str:
    cover_path = str(Path(downloads_dir) / str(uuid.uuid4()))
    try:
        download_file(image_url, cover_path)
    except Exception:
        Path(cover_path).unlink(missing_ok=True)
        raise
    return cover_path


def handle_cover_file(path: str, cfg: AppConfig) -> None:
    if not path:
        return
    if cfg.keep_cover and cfg.sort_by_context:
        Path(path).rename(Path(path).parent / "cover.jpg")
    else:
        Path(path).unlink(missing_ok=True)


def require_cover(cfg: AppConfig, respect_fix_tags: bool, respect_keep_cover: bool) -> bool:
    fix_tags = respect_fix_tags and cfg.fix_tags and (cfg.cover_size != "1400x1400" or cfg.quality != "lossless")
    keep_cover = respect_keep_cover and cfg.sort_by_context and cfg.keep_cover
    return fix_tags or keep_cover


def save_track(
    client: BeatportClient,
    track: Track,
    directory: str,
    cfg: AppConfig,
    active_files: set,
    active_files_lock,
    on_progress=None,
) -> str | None:
    download_info = client.download_track(track.id, cfg.quality)
    stream_quality = download_info.get("stream_quality", "")
    if stream_quality == ".128k.aac.mp4":
        ext, display_quality = ".m4a", "AAC 128kbps"
    elif stream_quality == ".256k.aac.mp4":
        ext, display_quality = ".m4a", "AAC 256kbps"
    elif stream_quality == ".flac":
        ext, display_quality = ".flac", "FLAC"
    else:
        raise RuntimeError(f"invalid stream quality: {stream_quality}")

    naming = NamingPreferences(
        template=cfg.track_file_template,
        whitespace=cfg.whitespace_character,
        artists_limit=cfg.artists_limit,
        artists_short_form=cfg.artists_short_form,
        track_number_padding=cfg.track_number_padding,
        key_system=cfg.key_system,
    )
    filename = track.filename(naming)
    file_path = f"{directory}/{filename}{ext}"

    if Path(file_path).exists():
        with active_files_lock:
            already_active = file_path in active_files
        if already_active:
            i = 1
            while True:
                candidate = f"{directory}/{filename} ({i}){ext}"
                if not Path(candidate).exists():
                    file_path = candidate
                    break
                i += 1
        else:
            if cfg.track_exists == "skip":
                return None
            if cfg.track_exists == "update":
                console.print(f"[dim][{track.store_url()}][/dim] updating tags")
                return file_path
            if cfg.track_exists == "error":
                raise TrackFileExistsError(file_path)

    with active_files_lock:
        active_files.add(file_path)

    info_display = f"{track.name} ({track.mix_name}) [{display_quality}]"
    console.print(f"Downloading {info_display}")

    try:
        download_file(download_info["location"], file_path, on_progress=on_progress)
    except BaseException:
        Path(file_path).unlink(missing_ok=True)
        raise

    console.print(f"[green]Finished downloading[/green] {info_display}")
    return file_path


def handle_track(
    client: BeatportClient,
    track: Track,
    downloads_dir: str,
    cover_path: str | None,
    cfg: AppConfig,
    active_files: set,
    active_files_lock,
    on_progress=None,
) -> str | None:
    location = save_track(client, track, downloads_dir, cfg, active_files, active_files_lock, on_progress=on_progress)
    if not location:
        return None
    tag_track(location, track, cover_path, cfg)
    return location


def track_matches_filter(track: Track, cfg: AppConfig) -> bool:
    if not (cfg.filter_genres or cfg.filter_subgenres or cfg.filter_artists or cfg.filter_publish_date_from):
        return True

    if cfg.filter_publish_date_from and track.publish_date < cfg.filter_publish_date_from:
        return False
    if cfg.filter_publish_date_to and track.publish_date > cfg.filter_publish_date_to:
        return False

    if cfg.filter_genres and track.genre.name.lower() not in {g.lower() for g in cfg.filter_genres}:
        return False

    if cfg.filter_subgenres:
        subgenre_name = track.subgenre.name if track.subgenre else ""
        if subgenre_name.lower() not in {g.lower() for g in cfg.filter_subgenres}:
            return False

    if cfg.filter_artists:
        track_artist_names = {a["name"].lower() for a in track.artists}
        if not track_artist_names.intersection(a.lower() for a in cfg.filter_artists):
            return False

    return True
