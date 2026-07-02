from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_COVER_SIZE = "1400x1400"

SUPPORTED_TRACK_EXISTS = ("error", "skip", "overwrite", "update")
SUPPORTED_KEY_SYSTEMS = ("standard", "standard-short", "openkey", "camelot")
SUPPORTED_TAG_MAPPING_FORMATS = ("flac", "m4a")

SUPPORTED_TAG_MAPPING_FIELDS = (
    "track_id", "track_url", "track_name", "track_artists", "track_remixers",
    "track_artists_limited", "track_remixers_limited", "track_number",
    "track_number_with_padding", "track_number_with_total", "track_genre",
    "track_subgenre", "track_genre_with_subgenre", "track_subgenre_or_genre",
    "track_key", "track_bpm", "track_isrc",
    "release_id", "release_url", "release_name", "release_artists", "release_remixers",
    "release_artists_limited", "release_remixers_limited", "release_date", "release_year",
    "release_track_count", "release_track_count_with_padding", "release_catalog_number",
    "release_upc", "release_label", "release_label_url",
)

DEFAULT_TAG_MAPPINGS = {
    "flac": {
        "track_id": "BEATPORT_TRACK_ID",
        "track_name": "TITLE",
        "track_artists": "ARTIST",
        "track_number": "TRACKNUMBER",
        "track_subgenre_or_genre": "GENRE",
        "track_key": "KEY",
        "track_bpm": "BPM",
        "track_isrc": "ISRC",
        "release_id": "BEATPORT_RELEASE_ID",
        "release_name": "ALBUM",
        "release_artists": "ALBUMARTIST",
        "release_date": "DATE",
        "release_track_count": "TOTALTRACKS",
        "release_catalog_number": "CATALOGNUMBER",
        "release_label": "LABEL",
    },
    "m4a": {
        "track_id": "BEATPORT_TRACK_ID",
        "track_name": "TITLE",
        "track_artists": "ARTIST",
        "track_number": "TRACKNUMBER",
        "track_genre": "GENRE",
        "track_key": "KEY",
        "track_bpm": "BPM",
        "track_isrc": "ISRC",
        "release_id": "BEATPORT_RELEASE_ID",
        "release_name": "ALBUM",
        "release_artists": "ALBUMARTIST",
        "release_date": "DATE",
        "release_track_count": "TOTALTRACKS",
        "release_catalog_number": "CATALOGNUMBER",
        "release_label": "LABEL",
    },
}

_DEFAULTS = dict(
    quality="lossless",
    write_error_log=False,
    show_progress=True,
    max_global_workers=15,
    max_download_workers=15,
    sort_by_context=False,
    sort_by_label=False,
    force_release_directories=False,
    track_exists="update",
    track_number_padding=2,
    release_directory_template="[{catalog_number}] {artists} - {name}",
    playlist_directory_template="{name} [{created_date}]",
    chart_directory_template="{name} [{published_date}]",
    label_directory_template="{name} [{updated_date}]",
    artist_directory_template="{name}",
    track_file_template="{number}. {artists} - {name} ({mix_name})",
    whitespace_character="",
    artists_limit=3,
    artists_short_form="VA",
    key_system="standard-short",
    cover_size=DEFAULT_COVER_SIZE,
    keep_cover=False,
    fix_tags=True,
    proxy="",
    skip_previously_downloaded=True,
    watch_interval_hours=6,
    notify_webhook_url="",
)


class ConfigError(ValueError):
    pass


@dataclass
class AppConfig:
    username: str = ""
    password: str = ""
    quality: str = "lossless"
    write_error_log: bool = False
    show_progress: bool = True

    max_global_workers: int = 15
    max_download_workers: int = 15

    downloads_directory: str = ""
    sort_by_context: bool = False
    sort_by_label: bool = False
    force_release_directories: bool = False
    track_exists: str = "update"
    track_number_padding: int = 2

    release_directory_template: str = "[{catalog_number}] {artists} - {name}"
    playlist_directory_template: str = "{name} [{created_date}]"
    chart_directory_template: str = "{name} [{published_date}]"
    label_directory_template: str = "{name} [{updated_date}]"
    artist_directory_template: str = "{name}"
    track_file_template: str = "{number}. {artists} - {name} ({mix_name})"
    whitespace_character: str = ""
    artists_limit: int = 3
    artists_short_form: str = "VA"
    key_system: str = "standard-short"

    cover_size: str = DEFAULT_COVER_SIZE
    keep_cover: bool = False
    fix_tags: bool = True

    tag_mappings: dict = field(default_factory=lambda: DEFAULT_TAG_MAPPINGS)

    proxy: str = ""

    filter_genres: list[str] = field(default_factory=list)
    filter_subgenres: list[str] = field(default_factory=list)
    filter_artists: list[str] = field(default_factory=list)
    filter_publish_date_from: str = ""
    filter_publish_date_to: str = ""

    skip_previously_downloaded: bool = True

    watched_labels: list[dict] = field(default_factory=list)
    watch_interval_hours: int = 6

    # Generic outbound notification hook — a plain HTTP POST, so it works with
    # Discord/Slack incoming webhooks, ntfy.sh, Gotify (URL includes its own
    # token query param), or any custom bot listening for a JSON POST. See
    # bpdl/notify.py for the exact payload shape.
    notify_webhook_url: str = ""


def _validate_tag_mappings(mappings: dict) -> None:
    for fmt, fields in mappings.items():
        if fmt not in SUPPORTED_TAG_MAPPING_FORMATS:
            raise ConfigError(f"invalid tag mapping format '{fmt}'")
        for field_name in fields:
            if field_name not in SUPPORTED_TAG_MAPPING_FIELDS:
                raise ConfigError(f"invalid tag mapping field '{field_name}'")


def parse(file_path: str | Path) -> AppConfig:
    with open(file_path, "r") as f:
        raw = yaml.safe_load(f) or {}

    merged = {**_DEFAULTS, **raw}
    cfg = AppConfig(
        username=merged.get("username", ""),
        password=merged.get("password", ""),
        quality=merged["quality"],
        write_error_log=merged["write_error_log"],
        show_progress=merged["show_progress"],
        max_global_workers=merged["max_global_workers"],
        max_download_workers=merged["max_download_workers"],
        downloads_directory=merged.get("downloads_directory", ""),
        sort_by_context=merged["sort_by_context"],
        sort_by_label=merged["sort_by_label"],
        force_release_directories=merged["force_release_directories"],
        track_exists=merged["track_exists"],
        track_number_padding=merged["track_number_padding"],
        release_directory_template=merged["release_directory_template"],
        playlist_directory_template=merged["playlist_directory_template"],
        chart_directory_template=merged["chart_directory_template"],
        label_directory_template=merged["label_directory_template"],
        artist_directory_template=merged["artist_directory_template"],
        track_file_template=merged["track_file_template"],
        whitespace_character=merged["whitespace_character"],
        artists_limit=merged["artists_limit"],
        artists_short_form=merged["artists_short_form"],
        key_system=merged["key_system"],
        cover_size=merged["cover_size"],
        keep_cover=merged["keep_cover"],
        fix_tags=merged["fix_tags"],
        tag_mappings=raw.get("tag_mappings") or DEFAULT_TAG_MAPPINGS,
        proxy=merged["proxy"],
        filter_genres=raw.get("filter_genres") or [],
        filter_subgenres=raw.get("filter_subgenres") or [],
        filter_artists=raw.get("filter_artists") or [],
        filter_publish_date_from=raw.get("filter_publish_date_from", ""),
        filter_publish_date_to=raw.get("filter_publish_date_to", ""),
        skip_previously_downloaded=merged["skip_previously_downloaded"],
        watched_labels=raw.get("watched_labels") or [],
        watch_interval_hours=merged["watch_interval_hours"],
        notify_webhook_url=merged["notify_webhook_url"],
    )

    if not cfg.username or not cfg.password:
        raise ConfigError("username or password is not provided")

    if raw.get("tag_mappings"):
        _validate_tag_mappings(cfg.tag_mappings)
        cfg.tag_mappings.setdefault("flac", DEFAULT_TAG_MAPPINGS["flac"])
        cfg.tag_mappings.setdefault("m4a", DEFAULT_TAG_MAPPINGS["m4a"])

    if cfg.key_system not in SUPPORTED_KEY_SYSTEMS:
        raise ConfigError("invalid key system")

    if not cfg.downloads_directory:
        raise ConfigError("no downloads directory provided")

    if cfg.track_exists not in SUPPORTED_TRACK_EXISTS:
        raise ConfigError("invalid track exists behavior")

    if not (0 <= cfg.track_number_padding <= 10):
        raise ConfigError("invalid track number padding")

    return cfg


def save(cfg: AppConfig, file_path: str | Path) -> None:
    """Persists the full config schema — every setting the TUI settings menu
    can touch, not just the handful a user is likely to change by hand."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "username": cfg.username,
        "password": cfg.password,
        "quality": cfg.quality,
        "write_error_log": cfg.write_error_log,
        "show_progress": cfg.show_progress,
        "max_global_workers": cfg.max_global_workers,
        "max_download_workers": cfg.max_download_workers,
        "downloads_directory": cfg.downloads_directory,
        "sort_by_context": cfg.sort_by_context,
        "sort_by_label": cfg.sort_by_label,
        "force_release_directories": cfg.force_release_directories,
        "track_exists": cfg.track_exists,
        "track_number_padding": cfg.track_number_padding,
        "release_directory_template": cfg.release_directory_template,
        "playlist_directory_template": cfg.playlist_directory_template,
        "chart_directory_template": cfg.chart_directory_template,
        "label_directory_template": cfg.label_directory_template,
        "artist_directory_template": cfg.artist_directory_template,
        "track_file_template": cfg.track_file_template,
        "whitespace_character": cfg.whitespace_character,
        "artists_limit": cfg.artists_limit,
        "artists_short_form": cfg.artists_short_form,
        "key_system": cfg.key_system,
        "cover_size": cfg.cover_size,
        "keep_cover": cfg.keep_cover,
        "fix_tags": cfg.fix_tags,
        "proxy": cfg.proxy,
        "skip_previously_downloaded": cfg.skip_previously_downloaded,
        "watched_labels": cfg.watched_labels,
        "watch_interval_hours": cfg.watch_interval_hours,
        "notify_webhook_url": cfg.notify_webhook_url,
    }
    with open(path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
