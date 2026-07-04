"""Unit tests for the pure/logic-heavy parts of bpdl — no network, no disk
(beyond tmp_path), no Beatport account. Run with: python -m pytest"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest import mock

import pytest

from bpdl.config import AppConfig
from bpdl.download import download_file, save_track, skippable_reason, track_matches_filter
from bpdl.links import InvalidUrlError, parse_url
from bpdl.models import Genre, Track
from bpdl.scanner import sanitize_params
from bpdl.templates import number_with_padding, parse_template, sanitize_path

# ---- links.parse_url --------------------------------------------------------


@pytest.mark.parametrize(
    "url, link_type, link_id, store",
    [
        ("https://www.beatport.com/track/some-slug/12345", "tracks", 12345, "beatport"),
        ("https://www.beatport.com/release/some-slug/67890", "releases", 67890, "beatport"),
        ("https://www.beatport.com/label/some-label/111", "labels", 111, "beatport"),
        ("https://www.beatport.com/artist/some-artist/222", "artists", 222, "beatport"),
        ("https://www.beatport.com/chart/some-chart/333", "charts", 333, "beatport"),
        ("https://www.beatport.com/en/track/some-slug/12345", "tracks", 12345, "beatport"),
        ("https://www.beatsource.com/track/some-slug/444", "tracks", 444, "beatsource"),
    ],
)
def test_parse_url_variants(url, link_type, link_id, store):
    link = parse_url(url)
    assert link.type == link_type
    assert link.id == link_id
    assert link.store == store


def test_parse_url_rejects_unknown_host():
    with pytest.raises(InvalidUrlError):
        parse_url("https://example.com/track/foo/1")


def test_parse_url_keeps_query_params():
    link = parse_url("https://www.beatport.com/label/some-label/111?page=3&per_page=25&order=asc")
    assert link.params == "page=3&per_page=25&order=asc"


# ---- scanner.sanitize_params ------------------------------------------------


def test_sanitize_params_strips_pagination():
    assert sanitize_params("page=3&per_page=25&order=asc") == "order=asc"
    assert sanitize_params("") == ""
    assert sanitize_params("order=asc") == "order=asc"


# ---- download.skippable_reason ----------------------------------------------


def test_skippable_reason():
    assert skippable_reason(RuntimeError("request failed with status code: 451 - territory")) == "territory restricted"
    assert skippable_reason(RuntimeError("not yet available")) == "pre-release"
    assert skippable_reason(RuntimeError("request failed with status code: 404 - x")) == "unavailable"
    assert skippable_reason(RuntimeError("something else broke")) == ""
    assert skippable_reason(None) == ""


# ---- download.track_matches_filter ------------------------------------------


def _track(genre="Techno", subgenre=None, artists=("A",), publish_date="2024-06-01"):
    return Track(
        id=1,
        name="T",
        genre=Genre(name=genre),
        subgenre=Genre(name=subgenre) if subgenre else None,
        artists=[{"name": a} for a in artists],
        publish_date=publish_date,
    )


def test_no_filters_matches_everything():
    assert track_matches_filter(_track(), AppConfig())


def test_genre_filter_case_insensitive():
    cfg = AppConfig(filter_genres=["techno"])
    assert track_matches_filter(_track(genre="Techno"), cfg)
    assert not track_matches_filter(_track(genre="House"), cfg)


def test_artist_filter():
    cfg = AppConfig(filter_artists=["someone"])
    assert track_matches_filter(_track(artists=("Someone", "Else")), cfg)
    assert not track_matches_filter(_track(artists=("Nobody",)), cfg)


def test_date_from_filter():
    cfg = AppConfig(filter_publish_date_from="2024-01-01")
    assert track_matches_filter(_track(publish_date="2024-06-01"), cfg)
    assert not track_matches_filter(_track(publish_date="2023-12-31"), cfg)


def test_date_to_only_filter_is_applied():
    # Regression: a filter consisting of only an end date used to be silently
    # ignored (filter_publish_date_to was missing from the "any filters set?"
    # early-return check).
    cfg = AppConfig(filter_publish_date_to="2020-01-01")
    assert not track_matches_filter(_track(publish_date="2024-06-01"), cfg)
    assert track_matches_filter(_track(publish_date="2019-05-05"), cfg)


# ---- templates ---------------------------------------------------------------


def test_parse_template():
    assert parse_template("{a} - {b}", {"a": "X", "b": "Y"}) == "X - Y"
    # unknown placeholders stay literal
    assert parse_template("{a} - {nope}", {"a": "X"}) == "X - {nope}"


def test_sanitize_path_removes_forbidden_chars():
    assert sanitize_path('a<b>c:d"e|f?g*h') == "abcdefgh"


def test_number_with_padding():
    assert number_with_padding(3, 12, 0) == "03"  # width from total
    assert number_with_padding(3, 12, 4) == "0003"


# ---- download.download_file --------------------------------------------------


def _fake_response(data: bytes):
    resp = mock.Mock()
    resp.status_code = 200
    resp.headers = {"Content-Length": str(len(data))}
    resp.iter_content = lambda chunk_size: iter([data])
    return resp


def test_download_file_overwrites_existing_destination(tmp_path):
    # Regression: rename() refuses to overwrite on Windows; replace() is atomic
    # on both platforms — track_exists="overwrite" depends on this.
    dest = tmp_path / "track.flac"
    dest.write_bytes(b"old contents")
    with mock.patch("bpdl.download._retry_get", return_value=_fake_response(b"new contents")):
        download_file(str(dest), str(dest))
    assert dest.read_bytes() == b"new contents"
    assert not (tmp_path / "track.flac.part").exists()


def test_download_file_cleans_up_part_on_failure(tmp_path):
    dest = tmp_path / "track.flac"
    resp = _fake_response(b"")

    def boom(chunk_size):
        raise IOError("connection reset")
        yield  # pragma: no cover

    resp.iter_content = boom
    with mock.patch("bpdl.download._retry_get", return_value=resp):
        with pytest.raises(IOError):
            download_file(str(dest), str(dest))
    assert not dest.exists()
    assert not (tmp_path / "track.flac.part").exists()


# ---- download.save_track concurrency ----------------------------------------


def _save_track_fixture(tmp_path, track_exists="update"):
    cfg = AppConfig(track_exists=track_exists, quality="lossless")
    track = _track()
    track.mix_name = "Original Mix"
    client = mock.Mock()
    client.download_track.return_value = {"stream_quality": ".flac", "location": "https://x/file.flac"}
    return cfg, track, client


def test_save_track_reserves_path_before_download(tmp_path):
    # Regression: two workers racing on the same target filename used to both
    # write the same .part file because the active-files reservation was only
    # consulted when the file already existed on disk.
    cfg, track, client = _save_track_fixture(tmp_path)
    active: set = set()
    lock = threading.Lock()
    written_paths = []

    def fake_download(url, destination, on_progress=None):
        written_paths.append(destination)
        Path(destination).write_bytes(b"x")

    with mock.patch("bpdl.download.download_file", side_effect=fake_download):
        p1 = save_track(client, track, str(tmp_path), cfg, active, lock)
        p2 = save_track(client, track, str(tmp_path), cfg, active, lock)

    assert p1 != p2, "second concurrent save of the same track must divert to a numbered variant"
    assert len(set(written_paths)) == 2


def test_save_track_releases_reservation_on_failure(tmp_path):
    cfg, track, client = _save_track_fixture(tmp_path)
    active: set = set()
    lock = threading.Lock()

    with mock.patch("bpdl.download.download_file", side_effect=IOError("boom")):
        with pytest.raises(IOError):
            save_track(client, track, str(tmp_path), cfg, active, lock)

    assert not active, "a failed download must not leave its path reserved"


def test_save_track_skip_and_update(tmp_path):
    cfg, track, client = _save_track_fixture(tmp_path, track_exists="skip")
    active: set = set()
    lock = threading.Lock()

    def fake_download(url, destination, on_progress=None):
        Path(destination).write_bytes(b"x")

    with mock.patch("bpdl.download.download_file", side_effect=fake_download):
        first = save_track(client, track, str(tmp_path), cfg, active, lock)
    assert first and Path(first).exists()

    # existing file + skip → None, nothing new reserved
    active.clear()
    assert save_track(client, track, str(tmp_path), cfg, active, lock) is None
    assert not active

    # existing file + update → same path returned for re-tagging, no download
    cfg.track_exists = "update"
    with mock.patch("bpdl.download.download_file", side_effect=AssertionError("must not download")):
        assert save_track(client, track, str(tmp_path), cfg, active, lock) == first


# ---- history: track-level watch helpers (artist watch-list) ----

def _history_at(tmp_path):
    from bpdl import history
    history._db_path = tmp_path / "history.sqlite3"
    history.init_db()
    return history


def test_track_baseline_is_seen_but_not_downloaded(tmp_path):
    history = _history_at(tmp_path)
    # a fresh track is neither seen nor downloaded
    assert not history.is_track_seen(555)
    assert not history.is_track_downloaded(555)
    # baselining marks it seen (so artist watch won't treat old catalogue as new)
    # without counting as a real download
    history.mark_track_baseline(555, 99, "Old Track", "Some Artist", "Old EP", "Some Label")
    assert history.is_track_seen(555)
    assert not history.is_track_downloaded(555)


def test_track_seen_ignores_zero_id(tmp_path):
    history = _history_at(tmp_path)
    assert not history.is_track_seen(0)
