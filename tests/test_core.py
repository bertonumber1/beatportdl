"""Unit tests for the pure/logic-heavy parts of bpdl — no network, no disk
(beyond tmp_path), no Beatport account. Run with: python -m pytest"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from bpdl import api
from bpdl.config import AppConfig
from bpdl.download import download_file, save_track, skippable_reason, track_matches_filter
from bpdl.links import InvalidUrlError, parse_url
from bpdl.models import Genre, Track
from bpdl.scanner import IncompletePagination, for_paginated, sanitize_params
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


# ---- scanner.for_paginated --------------------------------------------------


class _Page:
    """Minimal stand-in for api.Paginated — the only three fields a walk reads."""

    def __init__(self, results, nxt, count):
        self.results = results
        self.next = nxt
        self.count = count


def _walker(pages):
    """fetch_page over a canned list, recording which pages were asked for."""
    asked = []

    def fetch(entity_id, page, params):
        asked.append(page)
        return pages[page - 1]

    return fetch, asked


def test_for_paginated_walks_every_page():
    pages = [_Page([1, 2], "next", 5), _Page([3, 4], "next", 5), _Page([5], None, 5)]
    fetch, asked = _walker(pages)
    seen = []
    for_paginated(1, "", fetch, lambda item, i: seen.append(item))
    assert seen == [1, 2, 3, 4, 5]
    assert asked == [1, 2, 3]


def test_for_paginated_rejects_a_walk_that_ends_short():
    """A truncated response looks exactly like the end of a catalogue. Believing it
    is how a 1,984-release label quietly downloads as 280 releases, so the walk is
    only complete once it has seen as many items as the server said existed."""
    pages = [_Page([1, 2], "next", 100), _Page([3], None, 100)]
    fetch, asked = _walker(pages)
    seen = []
    with pytest.raises(IncompletePagination) as e:
        for_paginated(1, "", fetch, lambda item, i: seen.append(item))
    assert "3 of 100" in str(e.value)
    # page 2 was asked for twice: once walked, once to confirm before failing
    assert asked == [1, 2, 2]


def test_for_paginated_retries_the_short_page_without_double_processing():
    """The retry must not re-run process_item over items it already handled — those
    queue downloads and write baseline rows, so a replay is not free."""
    short = _Page([3], "", 5)
    full = _Page([3, 4], "next", 5)
    last = _Page([5], None, 5)
    order = [_Page([1, 2], "next", 5), short, full, last]
    calls = []

    def fetch(entity_id, page, params):
        calls.append(page)
        return order[len(calls) - 1]

    seen = []
    for_paginated(1, "", fetch, lambda item, i: seen.append(item))
    assert seen == [1, 2, 3, 4, 5]      # 3 walked once, not twice
    assert calls == [1, 2, 2, 3]


def test_for_paginated_accepts_an_end_with_no_reported_total():
    """Endpoints that report no count get the old behaviour — there is nothing to
    check the walk against, and refusing them would break every one of them."""
    fetch, _ = _walker([_Page([1], None, 0)])
    seen = []
    for_paginated(1, "", fetch, lambda item, i: seen.append(item))
    assert seen == [1]


def test_for_paginated_stop_does_not_trip_the_completeness_check():
    """An explicit user Stop is a deliberate short walk, not a broken one."""
    fetch, _ = _walker([_Page([1], "next", 100)])
    for_paginated(1, "", fetch, lambda item, i: None, should_stop=lambda: True)


def test_bulk_walks_ask_for_a_hundred_a_page():
    """Beatport serves 10 a page by default, which is ~200 sequential requests for a
    2,000-release label. Each one is a chance for the walk to be cut short."""
    from bpdl.api import WALK_PAGE_SIZE

    assert WALK_PAGE_SIZE == 100
    seen = []

    class _Client(api.BeatportClient):
        def __init__(self):
            pass

        def _paginated(self, endpoint, item_cls):
            seen.append(endpoint)
            return _Page([], None, 0)

    c = _Client()
    c.get_label_releases(27465, 2, "publish_date=2026-01-01:")
    c.get_artist_tracks(1, 1, "")
    c.get_release_tracks(1, 1, "")
    # the caller's own params come last, so an explicit per_page still wins
    c.get_label_releases(27465, 1, "order_by=-publish_date", per_page=1)
    assert seen[0] == "/catalog/labels/27465/releases/?page=2&per_page=100&publish_date=2026-01-01:"
    assert "per_page=100" in seen[1] and "per_page=100" in seen[2]
    assert seen[3] == "/catalog/labels/27465/releases/?page=1&per_page=1&order_by=-publish_date"


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
    with mock.patch("bpdl.download._retry_get", return_value=resp), \
         mock.patch("bpdl.download.time.sleep"):
        with pytest.raises(RuntimeError, match="transfer failed after"):
            download_file(str(dest), str(dest))
    assert not dest.exists()
    assert not (tmp_path / "track.flac.part").exists()


def test_download_file_retries_broken_stream(tmp_path):
    # A transient mid-stream failure (VPN SSL hiccup, connection reset) must not
    # fail the track: the next attempt gets a fresh response and completes.
    dest = tmp_path / "track.flac"

    def broken(chunk_size):
        yield b"partial"
        raise IOError("ssl record layer failure")

    bad = _fake_response(b"")
    bad.iter_content = broken
    good = _fake_response(b"full content")
    with mock.patch("bpdl.download._retry_get", side_effect=[bad, good]), \
         mock.patch("bpdl.download.time.sleep"):
        download_file(str(dest), str(dest))
    assert dest.read_bytes() == b"full content"
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


def test_label_queued_during_download_is_not_dropped():
    # Regression: queueing a second label while the first is downloading used to
    # vanish — _run_download snapshotted the queue at start and cleared it whole
    # at the end, so the mid-run addition was never processed and got wiped.
    from bpdl.webui import server

    processed: list[str] = []

    class FakeStats:
        def __init__(self):
            self.downloaded = 0
            self.skipped: dict = {}
            self.failed = 0

        def add_failed(self):
            self.failed += 1

    class FakeApp:
        def __init__(self, cfg, bp, bs, on_event=None):
            self.stats = FakeStats()

        def handle_url(self, url):
            processed.append(url)
            # Simulate the user queueing label B while label A is downloading.
            if url == "urlA":
                server.state.queue.append({"url": "urlB", "name": "Label B"})

        def shutdown(self, cancel_pending=False):
            pass

        def cancel(self):
            pass

    server.state.queue = [{"url": "urlA", "name": "Label A"}]
    server.state.downloading = False
    server.state.stop_requested = False
    try:
        with mock.patch.object(server, "App", FakeApp), \
                mock.patch.object(server.bus, "publish", lambda ev: None):
            server._run_download()

        assert processed == ["urlA", "urlB"]   # both got downloaded, in order
        assert server.state.queue == []        # nothing left dangling
        assert server.state.downloading is False
    finally:
        server.state.queue = []


# ---- watch-list: publish_date vs release date, and the per-label watermark ----


def _release(rid, name, street_date, publish_date):
    """Minimal Release built the way the API layer builds it, so the tests cover
    the real new_release_date/publish_date mapping rather than a hand-made object."""
    from bpdl.models import Release
    return Release.from_json(
        {"id": rid, "name": name, "new_release_date": street_date,
         "publish_date": publish_date, "label": {"id": 1, "name": "Test Label"}},
        "beatport",
    )


def test_release_parses_publish_date_separately_from_street_date():
    r = _release(1, "Reissue", "2001-05-12", "2025-09-02T00:00:00-06:00")
    assert r.date == "2001-05-12"       # original street date
    assert r.publish_date == "2025-09-02"  # when Beatport listed it


class _WatchHarness:
    """Drives server._check_watched_label against a fixed set of releases,
    capturing the params it fetches with and the URLs it tries to download."""

    def __init__(self, releases, sync=None):
        self.releases = releases
        self.params_used = []
        self.downloaded = []
        self.download_raises = False
        self.cfg_seen = None
        self.sync = sync            # a recorded full-label download, or None
        self.sync_writes = []

    def install(self, monkeypatch, entry, *, lookback=14, staging=""):
        from bpdl.webui import server

        harness = self

        class FakeClient:
            store = "beatport"

            def get_label_releases(self, label_id, page, params=""):
                raise AssertionError("paging is driven by the patched for_paginated")

            def get_release(self, release_id):
                raise AssertionError("no pending releases in these tests")

        def fake_for_paginated(entity_id, params, fetch_page, process_item, should_stop=None):
            harness.params_used.append(params)
            for i, rel in enumerate(harness.releases):
                process_item(rel, i)

        class FakeApp:
            def __init__(self, cfg, bp, bs, on_event=None):
                harness.cfg_seen = cfg
                self.stats = mock.Mock(downloaded=len(harness.releases))

            def handle_url(self, url):
                if harness.download_raises:
                    raise RuntimeError("boom")
                harness.downloaded.append(url)

            def shutdown(self, cancel_pending=False):
                pass

        cfg = AppConfig(username="u", password="p", watch_lookback_days=lookback,
                        watch_downloads_directory=staging, watched_labels=[entry])
        monkeypatch.setattr(server.state, "cfg", cfg)
        monkeypatch.setattr(server.state, "config_path", "")   # no config writes in tests
        monkeypatch.setattr(server, "_client_for", lambda store: FakeClient())
        monkeypatch.setattr(server, "for_paginated", fake_for_paginated)
        monkeypatch.setattr(server, "App", FakeApp)
        monkeypatch.setattr(server.bus, "publish", lambda ev: None)
        monkeypatch.setattr(server.history, "is_release_seen", lambda rid: False)
        monkeypatch.setattr(server.history, "mark_release_baseline",
                            lambda *a, **k: harness.__dict__.setdefault("baselined", []).append(a[0]))
        monkeypatch.setattr(server.history, "add_pending_release", lambda *a, **k: None)
        monkeypatch.setattr(server.history, "get_due_pending", lambda url: [])
        monkeypatch.setattr(server.history, "remove_pending", lambda *a: None)
        # Stub the full-download mark too, or these tests open the real history DB
        # (and create one on disk) just to read a table they never populate.
        monkeypatch.setattr(server.history, "get_label_sync", lambda lid: harness.sync)
        monkeypatch.setattr(server.history, "record_label_sync",
                            lambda **kw: harness.sync_writes.append(kw))
        return server


def test_watch_grabs_back_catalogue_upload_dated_decades_ago(monkeypatch):
    """The bug this replaces: a label uploading its back catalogue produces
    releases with a decades-old street date but a fresh publish_date. Judging
    newness by street date baselined every one of them and downloaded nothing."""
    rel = _release(10, "Nightmare Ravers", "2001-05-12", "2026-07-28")
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-07-01", "last_publish_date": "2026-07-20"}
    h = _WatchHarness([rel])
    server = h.install(monkeypatch, entry)

    result = server._check_watched_label(entry)

    assert result["new_releases"] == 1
    assert h.downloaded == [rel.store_url()]


def test_watch_baselines_release_beatport_listed_before_we_started_watching(monkeypatch):
    rel = _release(11, "Old Upload", "2001-05-12", "2026-06-01")
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-07-01", "last_publish_date": "2026-07-20"}
    h = _WatchHarness([rel])
    server = h.install(monkeypatch, entry)

    result = server._check_watched_label(entry)

    assert result["new_releases"] == 0
    assert h.downloaded == []
    assert h.baselined == [11]


def test_watch_first_check_walks_whole_catalogue_then_sets_watermark(monkeypatch):
    """No watermark = never checked, so fetch unfiltered (the back catalogue has
    to be walked once to be baselined). The watermark is set from that walk."""
    rels = [_release(1, "A", "2020-01-01", "2026-06-10"),
            _release(2, "B", "2020-01-01", "2026-06-25")]
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-07-01"}
    h = _WatchHarness(rels)
    server = h.install(monkeypatch, entry)

    server._check_watched_label(entry)

    assert h.params_used == [""]                      # unfiltered full walk
    assert entry["last_publish_date"] == "2026-06-25"  # newest publish_date seen


def test_watch_second_check_fetches_only_since_watermark_less_lookback(monkeypatch):
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-01-01", "last_publish_date": "2026-07-20"}
    h = _WatchHarness([])
    server = h.install(monkeypatch, entry, lookback=14)

    server._check_watched_label(entry)

    assert h.params_used == ["publish_date=2026-07-06:"]  # 2026-07-20 minus 14d


def test_watch_watermark_never_advances_past_a_failed_download(monkeypatch):
    """Advancing past a release we failed to fetch would put it outside every
    future fetch window — it would never be offered again."""
    rel = _release(12, "New", "2026-07-25", "2026-07-28")
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-07-01", "last_publish_date": "2026-07-20"}
    h = _WatchHarness([rel])
    h.download_raises = True
    server = h.install(monkeypatch, entry)

    server._check_watched_label(entry)

    assert entry["last_publish_date"] == "2026-07-20"  # unchanged


def test_watch_downloads_go_to_staging_folder_sorted_by_label(monkeypatch, tmp_path):
    staging = str(tmp_path / "-label-releases")
    rel = _release(13, "New", "2026-07-25", "2026-07-28")
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-07-01", "last_publish_date": "2026-07-20"}
    h = _WatchHarness([rel])
    server = h.install(monkeypatch, entry, staging=staging)

    server._check_watched_label(entry)

    assert h.cfg_seen.downloads_directory == staging
    assert h.cfg_seen.sort_by_label is True          # one subfolder per label
    assert server.state.cfg.downloads_directory != staging  # library cfg untouched
    assert Path(staging).is_dir()


# ---- fully-downloaded labels: the mark, and topping up from it ------------------


def test_watch_measures_from_full_download_date_not_from_when_watching_started(monkeypatch):
    """The point of recording a full download: a label grabbed in full in March and
    only added to the watch list in July must still treat a release Beatport
    published in May as new. Judged by watched_since alone it looks like
    pre-existing catalogue and gets baselined away, so it is never downloaded."""
    rel = _release(20, "Published In May", "2026-05-04", "2026-05-04")
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-07-01"}
    h = _WatchHarness([rel], sync={"label_name": "X", "synced_through": "2026-03-01"})
    server = h.install(monkeypatch, entry)

    result = server._check_watched_label(entry)

    assert result["new_releases"] == 1
    assert h.downloaded == [rel.store_url()]
    assert not getattr(h, "baselined", [])


def test_update_latest_fetches_only_what_was_published_since_the_mark(monkeypatch):
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-01-01"}
    h = _WatchHarness([], sync={"label_name": "X", "synced_through": "2026-07-20"})
    server = h.install(monkeypatch, entry, lookback=14)

    server._check_watched_label(entry)

    assert h.params_used == ["publish_date=2026-07-06:"]   # mark minus 14d lookback


def test_full_download_mark_outranks_a_stale_watch_watermark(monkeypatch):
    """Both marks exist and disagree — the recorded full download is the one that
    says the files are actually on disk, so it wins."""
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-01-01", "last_publish_date": "2026-02-01"}
    h = _WatchHarness([], sync={"label_name": "X", "synced_through": "2026-07-20"})
    server = h.install(monkeypatch, entry, lookback=0)

    server._check_watched_label(entry)

    assert h.params_used == ["publish_date=2026-07-20:"]


def test_successful_update_advances_the_full_download_mark(monkeypatch):
    rel = _release(21, "Brand New", "2026-07-25", "2026-07-28")
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-07-01"}
    h = _WatchHarness([rel], sync={"label_name": "X", "synced_through": "2026-07-20"})
    server = h.install(monkeypatch, entry)

    server._check_watched_label(entry)

    assert [w["synced_through"] for w in h.sync_writes] == ["2026-07-28"]
    assert h.sync_writes[0]["releases"] == 1


def test_failed_update_leaves_the_full_download_mark_alone(monkeypatch):
    """Same reasoning as the watermark: advancing past a release we failed to fetch
    would put it outside every future window."""
    rel = _release(22, "Brand New", "2026-07-25", "2026-07-28")
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-07-01"}
    h = _WatchHarness([rel], sync={"label_name": "X", "synced_through": "2026-07-20"})
    h.download_raises = True
    server = h.install(monkeypatch, entry)

    server._check_watched_label(entry)

    assert h.sync_writes == []


def test_unfiltered_whole_label_download_records_the_mark(monkeypatch):
    from bpdl.webui import server

    writes = []
    monkeypatch.setattr(server.history, "record_label_sync", lambda **kw: writes.append(kw))
    monkeypatch.setattr(server, "_newest_publish_date", lambda client, lid: "2026-07-29")
    monkeypatch.setattr(server, "_client_for", lambda store: object())
    monkeypatch.setattr(server.bus, "publish", lambda ev: None)
    # Auto-watch is a separate concern with its own tests; keep this one on the mark.
    monkeypatch.setattr(server.state, "cfg",
                        AppConfig(username="u", password="p", auto_watch_labels=False))
    monkeypatch.setattr(server.state, "config_path", "")

    item = {"url": "https://www.beatport.com/label/x/1", "type": "labels", "id": 1,
            "store": "beatport", "name": "X", "filters": None}
    server._record_full_label_download(item, mock.Mock(stats=mock.Mock(failed=0, downloaded=42)))

    assert len(writes) == 1
    assert writes[0]["synced_through"] == "2026-07-29"
    assert writes[0]["tracks"] == 42


def test_filtered_label_download_is_not_recorded_as_a_full_one(monkeypatch):
    """A filtered grab covers a slice of the catalogue. Marking it complete would
    make every later update start after releases that were never fetched."""
    from bpdl.webui import server

    writes = []
    monkeypatch.setattr(server.history, "record_label_sync", lambda **kw: writes.append(kw))
    monkeypatch.setattr(server, "_newest_publish_date", lambda client, lid: "2026-07-29")

    item = {"url": "https://www.beatport.com/label/x/1", "type": "labels", "id": 1,
            "store": "beatport", "name": "X", "filters": {"genres": [8]}}
    server._record_full_label_download(item, mock.Mock(stats=mock.Mock(failed=0, downloaded=3)))

    assert writes == []


def test_label_download_with_failures_is_not_recorded_as_a_full_one(monkeypatch):
    from bpdl.webui import server

    writes = []
    monkeypatch.setattr(server.history, "record_label_sync", lambda **kw: writes.append(kw))
    monkeypatch.setattr(server, "_newest_publish_date", lambda client, lid: "2026-07-29")

    item = {"url": "https://www.beatport.com/label/x/1", "type": "labels", "id": 1,
            "store": "beatport", "name": "X", "filters": None}
    server._record_full_label_download(item, mock.Mock(stats=mock.Mock(failed=2, downloaded=40)))

    assert writes == []


def test_sync_mark_never_rewinds(tmp_path, monkeypatch):
    from bpdl import history

    monkeypatch.setattr(history, "_path", lambda: tmp_path / "h.sqlite3")
    history.init_db()
    history.record_label_sync(1, "beatport", "u", "X", "2026-07-20", tracks=10)
    history.record_label_sync(1, "beatport", "u", "X", "2026-01-01", releases=2, tracks=5)

    row = history.get_label_sync(1)
    assert row["synced_through"] == "2026-07-20"   # not rewound
    assert row["tracks"] == 15                      # but counters still accumulate
    assert row["releases"] == 2


def test_forget_label_sync_removes_the_mark(tmp_path, monkeypatch):
    from bpdl import history

    monkeypatch.setattr(history, "_path", lambda: tmp_path / "h.sqlite3")
    history.init_db()
    history.record_label_sync(1, "beatport", "u", "X", "2026-07-20")
    assert history.get_label_sync(1) is not None

    history.forget_label_sync(1)
    assert history.get_label_sync(1) is None


def test_watch_from_reaches_back_behind_the_watermark(monkeypatch):
    """An explicit 'from' is a request to go and fetch history, so the fetch window
    must widen past the watermark — otherwise Beatport is never asked for anything
    older than the last check and the range silently returns nothing."""
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-07-29", "last_publish_date": "2026-07-31",
             "watch_from": "2026-01-01"}
    h = _WatchHarness([], sync=None)
    server = h.install(monkeypatch, entry, lookback=14)

    server._check_watched_label(entry)

    assert h.params_used == ["publish_date=2026-01-01:"]


def test_watch_from_downloads_releases_older_than_watched_since(monkeypatch):
    """The back catalogue inside the range is wanted, not baselined away."""
    old = _release(31, "March Release", "2026-03-04", "2026-03-04")
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-07-29", "watch_from": "2026-01-01"}
    h = _WatchHarness([old], sync=None)
    server = h.install(monkeypatch, entry, lookback=0)

    result = server._check_watched_label(entry)

    assert result["new_releases"] == 1
    assert h.downloaded == [old.store_url()]
    assert not getattr(h, "baselined", [])


def test_watch_to_caps_the_far_end_of_the_range(monkeypatch):
    """Past 'to' is out of scope: baselined, never downloaded."""
    inside = _release(32, "In Range", "2026-02-02", "2026-02-02")
    after = _release(33, "Too New", "2026-06-06", "2026-06-06")
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-01-01",
             "watch_from": "2026-01-01", "watch_to": "2026-03-31"}
    h = _WatchHarness([inside, after], sync=None)
    server = h.install(monkeypatch, entry, lookback=0)

    result = server._check_watched_label(entry)

    assert result["new_releases"] == 1
    assert h.downloaded == [inside.store_url()]
    assert getattr(h, "baselined", []) == [after.id]


def test_watch_from_outranks_a_full_download_mark(monkeypatch):
    """The sync mark normally wins, but an explicit 'from' is the stronger, newer
    human instruction — backfilling behind a full download has to be possible."""
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-07-01", "watch_from": "2026-01-01"}
    h = _WatchHarness([], sync={"label_name": "X", "synced_through": "2026-07-20"})
    server = h.install(monkeypatch, entry, lookback=0)

    server._check_watched_label(entry)

    assert h.params_used == ["publish_date=2026-01-01:"]


def test_check_can_be_limited_to_one_label(monkeypatch):
    """Setting a range is a 'do it now' instruction, so it must be able to check
    that label alone rather than sweeping everything that is watched."""
    rel = _release(41, "In Range", "2026-02-02", "2026-02-02")
    entry = {"url": "https://www.beatport.com/label/x/1", "name": "X",
             "watched_since": "2026-01-01", "watch_from": "2026-01-01"}
    other = {"url": "https://www.beatport.com/label/y/2", "name": "Y",
             "watched_since": "2026-01-01"}
    h = _WatchHarness([rel], sync=None)
    server = h.install(monkeypatch, entry, lookback=0)
    server.state.cfg.watched_labels = [entry, other]
    monkeypatch.setattr(server.state, "login_status", "ok")
    monkeypatch.setattr(server.state, "downloading", False)
    monkeypatch.setattr(server.state, "watch_checking", False)
    monkeypatch.setattr(server, "_apply_filters", lambda f: None)

    server._run_watch_check(only=[entry])

    # One label checked, not both — a second pass would fetch twice.
    assert len(h.params_used) == 1
    assert h.downloaded == [rel.store_url()]


# ---- "download it as is": items stuck waiting on the filter wizard ----------------


def test_bypass_pending_unblocks_items_waiting_on_the_wizard():
    # Regression: a label queued whole (no filters) sat at needs_wizard=True and
    # /api/download/start refused with "finish choosing filters" — with no action
    # anywhere that would just download it as it is.
    from bpdl.webui import server

    server.state.queue = [
        {"url": "urlA", "name": "Label A", "needs_wizard": True, "filters": None},
        {"url": "urlB", "name": "Release B", "needs_wizard": False, "filters": None},
    ]
    try:
        with mock.patch.object(server, "_publish_queue", lambda: None):
            out = server.bypass_pending()

        assert out["unblocked"] == 1
        assert server.state.queue[0]["needs_wizard"] is False
        assert server.state.queue[0]["filters"] is None   # unfiltered = the whole thing
        # the already-ready item is untouched
        assert server.state.queue[1]["needs_wizard"] is False
        # and start's guard is now satisfied
        assert any(not q.get("needs_wizard") for q in server.state.queue)
    finally:
        server.state.queue = []


def test_set_filters_follows_the_url_when_the_queue_has_shifted():
    # Regression: the wizard addressed items by POSITION. When the queue shifted
    # under an open wizard (a finished item is removed by _run_download), the
    # stale index configured the WRONG item — or 404'd, which the UI never showed,
    # so the label stayed "needing filters" however often you chose queue-everything.
    from bpdl.webui import server

    server.state.queue = [
        {"url": "urlB", "name": "Label B", "needs_wizard": True, "filters": None},
    ]
    try:
        with mock.patch.object(server, "_publish_queue", lambda: None):
            # The UI opened the wizard when Label B was at index 1; index 0 has
            # since been removed, so position 1 no longer exists.
            payload = server.FiltersPayload(bypass=True, url="urlB")
            out = server.set_filters(1, payload)

        assert out["item"]["url"] == "urlB"
        assert server.state.queue[0]["needs_wizard"] is False
    finally:
        server.state.queue = []


def test_set_filters_still_404s_for_a_url_that_is_not_queued():
    from bpdl.webui import server

    server.state.queue = [{"url": "urlA", "name": "Label A", "needs_wizard": True}]
    try:
        with pytest.raises(server.HTTPException):
            server.set_filters(0, server.FiltersPayload(bypass=True, url="urlGONE"))
    finally:
        server.state.queue = []


# ---- folder rename / rescan -------------------------------------------------

def test_artists_display_collapses_a_joined_tag_to_the_short_form():
    from bpdl.rename import artists_display

    # One tag value holding ten artists is still ten artists. Counting tag VALUES sees
    # one and spells them all out, which is what produced a 250-char truncated folder
    # name where a real download writes "VA".
    joined = ["Ross Homson, Brian Felton, BK, Andy Farley, Nik Denton, Ben Stevens"]
    assert artists_display(joined, limit=3, short_form="VA") == "VA"
    assert artists_display(["A vs B"], limit=3, short_form="VA") == "A vs B"
    assert artists_display(["Ben Stevens, Tenchy"], limit=3, short_form="VA") == "Ben Stevens, Tenchy"


def test_artists_display_dedups_repeats_before_counting():
    from bpdl.rename import artists_display

    # A compilation repeating one artist across tracks must not tip over the limit.
    assert artists_display(["Ben Stevens", "Ben Stevens", "Tenchy"], limit=3,
                           short_form="VA") == "Ben Stevens, Tenchy"


def test_plan_leaves_folders_alone_when_tags_cannot_fill_the_template(tmp_path, monkeypatch):
    from bpdl import rename

    d = tmp_path / "Some Release"
    d.mkdir()
    (d / "01. x.flac").write_bytes(b"")
    monkeypatch.setattr(rename, "read_values",
                        lambda *a, **k: {"name": "Some Release", "artists": "A"})

    cfg = SimpleNamespace(release_directory_template="{name} [{upc}]",
                          whitespace_character="", artists_limit=3, artists_short_form="VA")
    rows = rename.plan(str(tmp_path), cfg)

    # {upc} is never embedded in a tag, so the name cannot be rebuilt offline. The folder
    # must be reported, NOT renamed to something containing a literal "{upc}".
    assert len(rows) == 1
    assert rows[0].new == ""
    assert rows[0].missing == ["upc"]
    assert not rows[0].changed


def test_plan_renders_the_current_template_from_tags(tmp_path, monkeypatch):
    from bpdl import rename

    d = tmp_path / "Mace In Your Face (2014) [Fireball Recordings - 5052653878845]"
    d.mkdir()
    (d / "01. x.flac").write_bytes(b"")
    monkeypatch.setattr(rename, "read_values", lambda *a, **k: {
        "name": "Mace In Your Face", "artists": "A vs B", "catalog_number": "FBR189"})

    cfg = SimpleNamespace(release_directory_template="[{catalog_number}] {artists} - {name}",
                          whitespace_character="", artists_limit=3, artists_short_form="VA")
    rows = rename.plan(str(tmp_path), cfg)

    assert rows[0].new == "[FBR189] A vs B - Mace In Your Face"
    assert rows[0].changed


def test_apply_never_overwrites_an_existing_folder(tmp_path):
    from bpdl import rename

    src = tmp_path / "old name"
    src.mkdir()
    (tmp_path / "taken").mkdir()
    rows = [rename.Row(str(src), "taken")]

    done, problems = rename.apply(rows)

    assert done == 0
    assert src.exists()                       # the source is still there, untouched
    assert problems and "already exists" in problems[0]


def test_record_label_sync_never_rewinds_by_accident(tmp_path, monkeypatch):
    from bpdl import history

    monkeypatch.setattr(history, "_db_path", tmp_path / "h.sqlite3")
    history.init_db()
    history.record_label_sync(1, "beatport", "u", "L", synced_through="2026-05-01")
    # An incremental update that happens to cover an older window must not pull the mark
    # back: everything between would fall inside every future fetch window again.
    history.record_label_sync(1, "beatport", "u", "L", synced_through="2026-01-01")
    assert history.get_label_sync(1)["synced_through"] == "2026-05-01"


def test_record_label_sync_rewinds_only_when_explicitly_allowed(tmp_path, monkeypatch):
    from bpdl import history

    monkeypatch.setattr(history, "_db_path", tmp_path / "h.sqlite3")
    history.init_db()
    history.record_label_sync(1, "beatport", "u", "L", synced_through="2026-05-01")
    # A person stating how far their copy actually goes is the one case where an earlier
    # date is the truth; claiming more than is held silently skips the gap.
    history.record_label_sync(1, "beatport", "u", "L", synced_through="2020-01-01",
                              allow_rewind=True)
    assert history.get_label_sync(1)["synced_through"] == "2020-01-01"


def test_clear_watch_keeps_the_sync_marks(monkeypatch):
    from bpdl.webui import server

    saved = {}
    monkeypatch.setattr(server.config_module, "save", lambda *a: saved.setdefault("n", 0))
    monkeypatch.setattr(server.history, "get_all_pending", lambda url: [])
    monkeypatch.setattr(server, "_sync_for_url", lambda url: None)
    server.state.cfg.watched_labels = [{"url": "a", "name": "A"}]
    server.state.cfg.watched_artists = [{"url": "b", "name": "B"}]
    try:
        out = server.clear_watch()
        # Unwatching is not the same as forgetting what is on disk: the marks record
        # what is actually held, so re-watching later resumes instead of baselining.
        assert out["removed"] == 2
        assert server.state.cfg.watched_labels == []
        assert server.state.cfg.watched_artists == []
    finally:
        server.state.cfg.watched_labels = []
        server.state.cfg.watched_artists = []


# --- per-label watch destination -------------------------------------------------
# A label's releases have no single home in the library, so the destination is
# stated per label rather than inferred. These pin the two things that make that
# safe: the path actually reaches the downloader, and a bad path is refused loudly
# instead of silently filing music somewhere unintended.

def test_valid_destination_accepts_absolute_and_blank():
    from bpdl.webui import server
    assert server._valid_destination("/music/MAKINA/LABELS_&_USBs/DNZ_RECORDS") == \
        "/music/MAKINA/LABELS_&_USBs/DNZ_RECORDS"
    assert server._valid_destination("/downloads/x/") == "/downloads/x"   # trailing / trimmed
    assert server._valid_destination("") == ""
    assert server._valid_destination(None) == ""


def test_destination_translates_host_path_to_mounted_path():
    """The host path is what the file manager shows, so it is the natural thing to
    type — but this process only ever sees the mount. Saving it unchanged failed six
    hours later as '[Errno 13] Permission denied: /mnt/usb-a' in a status field."""
    from bpdl.webui import server
    assert server._valid_destination(
        "/mnt/usb-a/drive/Music/UK_&_SPANISH_MAKINA_[FLAC]/LABELS_&_USBs/DNZ_RECORDS"
    ) == "/music/UK_&_SPANISH_MAKINA_[FLAC]/LABELS_&_USBs/DNZ_RECORDS"
    assert server._valid_destination(
        "/mnt/usb-b/drive/downloads/-label-releases") == "/downloads/-label-releases"
    # usb-a's downloads folder is mounted separately as the archive; mapping it to
    # /downloads would silently write to the wrong drive.
    assert server._valid_destination(
        "/mnt/usb-a/drive/downloads/old") == "/downloads-archive/old"
    # already-correct paths are untouched
    assert server._valid_destination("/music/GENRE/Label") == "/music/GENRE/Label"


def test_destination_rejects_unreachable_host_path():
    """A /mnt path that is neither mapped nor present cannot be written to, so it is
    refused while the person is looking at the form rather than on the next sweep."""
    from bpdl.webui import server
    from fastapi import HTTPException
    import pytest as _pytest
    with _pytest.raises(HTTPException) as e:
        server._valid_destination("/mnt/nowhere/Some Label")
    assert "/music" in str(e.value.detail)


def test_destination_prefix_match_respects_boundaries():
    """'/mnt/usb-a/drive/MusicOther' must not be rewritten by the '/drive/Music' rule."""
    from bpdl.webui import server
    from fastapi import HTTPException
    import pytest as _pytest
    with _pytest.raises(HTTPException):
        server._valid_destination("/mnt/usb-a/drive/MusicOther/Label")


def test_valid_destination_rejects_relative_path():
    from bpdl.webui import server
    from fastapi import HTTPException
    import pytest as _pytest
    with _pytest.raises(HTTPException) as e:
        server._valid_destination("music/MAKINA")
    assert e.value.status_code == 400


def test_watch_cfg_prefers_per_label_destination_over_staging(monkeypatch, tmp_path):
    """The per-label folder IS the label's folder, so sort_by_label must be off —
    otherwise releases land in <dest>/<Label>/ and the chosen path is wrong by one
    level."""
    from bpdl.webui import server
    from bpdl.config import AppConfig
    dest = tmp_path / "music" / "MAKINA" / "DNZ"
    dest.mkdir(parents=True)
    staging = tmp_path / "staging"
    monkeypatch.setattr(server.state, "cfg",
                        AppConfig(username="u", password="p",
                                  watch_downloads_directory=str(staging)))
    cfg = server._watch_cfg({"destination": str(dest)})
    assert cfg.downloads_directory == str(dest)
    assert cfg.sort_by_label is False


def test_watch_cfg_stages_instead_of_recreating_a_vanished_destination(monkeypatch, tmp_path):
    """The destination folder is gone — reorganised away, or its drive is not
    mounted. Re-creating it files the download into a path nobody opens again, so
    the releases go to staging and stay visible instead."""
    from bpdl.webui import server
    from bpdl.config import AppConfig
    dest = tmp_path / "music" / "MOVED_AWAY"          # deliberately never created
    staging = tmp_path / "staging"
    monkeypatch.setattr(server.state, "cfg",
                        AppConfig(username="u", password="p",
                                  watch_downloads_directory=str(staging)))
    cfg = server._watch_cfg({"destination": str(dest)})
    assert cfg.downloads_directory == str(staging)
    assert not dest.exists()


def test_watch_cfg_falls_back_to_staging_when_no_destination(monkeypatch, tmp_path):
    from bpdl.webui import server
    from bpdl.config import AppConfig
    staging = tmp_path / "staging"
    monkeypatch.setattr(server.state, "cfg",
                        AppConfig(username="u", password="p",
                                  watch_downloads_directory=str(staging)))
    cfg = server._watch_cfg({"url": "x"})            # entry with no destination
    assert cfg.downloads_directory == str(staging)
    assert cfg.sort_by_label is True                 # staging still splits per label


def test_watched_label_downloads_into_its_own_destination(monkeypatch, tmp_path):
    """End to end: the destination on the watch entry is the directory the
    downloader is actually handed."""
    dest = tmp_path / "music" / "MAKINA" / "LABELS_&_USBs" / "DNZ_RECORDS"
    dest.mkdir(parents=True)
    rel = _release(1, "New One", "2026-08-01", "2026-08-01T00:00:00-06:00")
    h = _WatchHarness([rel])
    entry = {"url": "https://www.beatport.com/label/dnz-records/1",
             "name": "DNZ Records", "watched_since": "2026-01-01",
             "destination": str(dest)}
    server = h.install(monkeypatch, entry, staging=str(tmp_path / "staging"))
    server._check_watched_label(entry)
    assert h.cfg_seen is not None
    assert h.cfg_seen.downloads_directory == str(dest)
    assert h.cfg_seen.sort_by_label is False


def test_library_destination_can_never_overwrite(monkeypatch, tmp_path):
    """An unattended watcher writing into the live library must only ever ADD.
    Even with the global setting on "overwrite", a per-label destination is pinned
    to skip — the existing file is the user's copy and is not ours to replace."""
    from bpdl.webui import server
    from bpdl.config import AppConfig
    dest = tmp_path / "music" / "MAKINA" / "DNZ"
    dest.mkdir(parents=True)
    monkeypatch.setattr(server.state, "cfg",
                        AppConfig(username="u", password="p",
                                  track_exists="overwrite",
                                  watch_downloads_directory=str(tmp_path / "staging")))
    cfg = server._watch_cfg({"destination": str(dest)})
    assert cfg.track_exists == "skip"
    # the shared staging folder is not the library, so it keeps the user's setting
    assert server._watch_cfg({}).track_exists == "overwrite"


# --- destination paths must work on Windows too ----------------------------------
# A naive startswith("/") check rejects every Windows path. bpdl ships to Windows
# users (see the sys.platform branches in paths.py), so all three absolute shapes
# have to be accepted while relative paths stay refused.

def test_destination_accepts_windows_drive_paths():
    from bpdl.webui import server
    assert server._valid_destination(r"C:\Music\DNZ Records") == r"C:\Music\DNZ Records"
    assert server._valid_destination("D:/Music/DNZ Records") == "D:/Music/DNZ Records"
    assert server._valid_destination(r"c:\music\label\\") == r"c:\music\label"


def test_destination_accepts_unc_share_paths():
    from bpdl.webui import server
    assert server._valid_destination(r"\\nas\music\Label") == r"\\nas\music\Label"
    assert server._valid_destination("//nas/music/Label") == "//nas/music/Label"


def test_destination_still_rejects_relative_paths():
    from bpdl.webui import server
    from fastapi import HTTPException
    import pytest as _pytest
    for bad in ("music/Label", r"Music\Label", "./music", "C:relative"):
        with _pytest.raises(HTTPException) as e:
            server._valid_destination(bad)
        assert e.value.status_code == 400, bad


def test_destination_never_strips_a_bare_root():
    """rstrip on separators would turn "C:\\" into "C:" and "/" into "", quietly
    pointing downloads somewhere else entirely."""
    from bpdl.webui import server
    assert server._valid_destination("/") == "/"
    assert server._valid_destination("C:\\") == "C:\\"


# --- following a label's folder when it moves ------------------------------------
# The library gets reorganised constantly, and a watch entry that remembers only a
# path is stale the moment a folder is moved or renamed. These cover the marker
# that makes the folder recognisable again, and — more importantly — the cases
# where it must REFUSE to guess.

@pytest.fixture(autouse=True)
def _clear_follow_index():
    """The marked-folder index is cached for a couple of minutes so one sweep does
    one walk. Tests build a new tree per case, so the cache has to go between them."""
    from bpdl import labelfollow
    labelfollow.invalidate()
    yield
    labelfollow.invalidate()


def _mark(folder, label_id=1, store="beatport", name="DNZ Records"):
    from bpdl import labelfollow
    folder.mkdir(parents=True, exist_ok=True)
    assert labelfollow.write_anchor(folder, label_id=label_id, store=store,
                                    label_url=f"https://www.beatport.com/label/x/{label_id}",
                                    label_name=name) == ""
    return folder


def test_marker_round_trips(tmp_path):
    from bpdl import labelfollow
    folder = _mark(tmp_path / "DNZ")
    data = labelfollow.read_anchor(folder)
    assert labelfollow.anchor_matches(data, 1, "beatport")
    assert not labelfollow.anchor_matches(data, 2, "beatport")
    assert not labelfollow.anchor_matches(data, 1, "beatsource")


def test_marker_survives_a_rename_and_a_move(tmp_path):
    """The whole point: the folder is renamed AND moved to another tree, and is
    still identified — which no amount of folder-name matching can do."""
    from bpdl import labelfollow
    downloads = tmp_path / "downloads"
    library = tmp_path / "music"
    library.mkdir()
    old = _mark(downloads / "beatport" / "DNZ Records [2026-08-26]")
    new = library / "MAKINA" / "LABELS_&_USBs" / "DNZ_RECORDS"
    new.parent.mkdir(parents=True)
    old.rename(new)

    res = labelfollow.resolve(str(old), 1, "beatport", [str(library), str(downloads)])
    assert res.status == "moved"
    assert res.path == str(new)


def test_a_stale_empty_folder_left_behind_does_not_win(tmp_path):
    """The failure this feature exists to prevent. An earlier run created the old
    path, the contents were moved out, and the empty shell remains. Trusting the
    recorded path files every future release into a folder nobody opens."""
    from bpdl import labelfollow
    library = tmp_path / "music"
    stale = library / "OLD_GENRE" / "DNZ"
    stale.mkdir(parents=True)                      # exists, but carries no marker
    real = _mark(library / "MAKINA" / "DNZ_RECORDS")

    res = labelfollow.resolve(str(stale), 1, "beatport", [str(library)])
    assert res.status == "moved"
    assert res.path == str(real)


def test_the_library_copy_beats_a_copy_left_in_downloads(tmp_path):
    """Copying rather than moving leaves the marker in two places. The library is
    where it was meant to end up, so root order decides it instead of an
    ambiguity the person has to resolve by hand."""
    from bpdl import labelfollow
    library = tmp_path / "music"
    downloads = tmp_path / "downloads"
    kept = _mark(library / "MAKINA" / "DNZ_RECORDS")
    _mark(downloads / "beatport" / "DNZ Records [2026-08-26]")

    res = labelfollow.resolve(str(downloads / "gone"), 1, "beatport",
                              [str(library), str(downloads)])
    assert res.status == "moved"
    assert res.path == str(kept)


def test_two_marked_folders_in_the_same_root_refuse_to_guess(tmp_path):
    from bpdl import labelfollow
    library = tmp_path / "music"
    _mark(library / "GABBER" / "5th Gear")
    _mark(library / "MULTI_GENRE" / "5th Gear")

    res = labelfollow.resolve(str(library / "gone"), 1, "beatport", [str(library)])
    assert res.status == "ambiguous"
    assert res.path == ""                      # no path: the caller uses staging
    assert len(res.candidates) == 2


def test_a_folder_marked_for_another_label_is_never_written_into(tmp_path):
    from bpdl import labelfollow
    library = tmp_path / "music"
    theirs = _mark(library / "MAKINA" / "SOMEONE_ELSE", label_id=99, name="Other")

    res = labelfollow.resolve(str(theirs), 1, "beatport", [str(library)])
    assert res.status == "conflict"
    assert res.path == ""
    # and the marker is left exactly as it was
    assert labelfollow.read_anchor(theirs)["label_id"] == 99
    assert labelfollow.write_anchor(theirs, label_id=1, store="beatport") != ""
    assert labelfollow.read_anchor(theirs)["label_id"] == 99


def test_an_unmarked_folder_that_is_still_there_gets_adopted(tmp_path):
    """Watch entries created before markers existed still name a real folder.
    Nothing else claims the label, so the folder is claimed rather than abandoned."""
    from bpdl import labelfollow
    library = tmp_path / "music"
    here = library / "MAKINA" / "DNZ"
    here.mkdir(parents=True)

    res = labelfollow.resolve(str(here), 1, "beatport", [str(library)])
    assert res.status == "adopted"
    assert res.path == str(here)


def test_a_folder_that_is_simply_gone_reports_lost(tmp_path):
    from bpdl import labelfollow
    library = tmp_path / "music"
    library.mkdir()
    res = labelfollow.resolve(str(library / "NEVER_EXISTED"), 1, "beatport", [str(library)])
    assert res.status == "lost"
    assert res.path == ""


def test_an_unfinished_scan_is_not_reported_as_lost(tmp_path):
    """A sleeping USB disk stalls the walk. Calling that 'lost' and clearing the
    destination would turn a slow disk into a permanently forgotten folder."""
    from bpdl import labelfollow
    library = tmp_path / "music"
    _mark(library / "MAKINA" / "DNZ")
    res = labelfollow.resolve(str(library / "gone"), 1, "beatport", [str(library)],
                              budget=-1.0, use_cache=False)
    assert res.status == "unscanned"
    assert res.path == ""


def test_the_index_does_not_descend_into_a_marked_folder(tmp_path):
    """A label folder holds releases, not other labels. Stopping there is what
    keeps a 40,000-folder library cheap to scan."""
    from bpdl import labelfollow
    library = tmp_path / "music"
    outer = _mark(library / "MAKINA" / "DNZ")
    _mark(outer / "[DNZ001] Someone - Thing", label_id=1)   # would be a duplicate hit
    found, complete = labelfollow.find(1, "beatport", [str(library)], use_cache=False)
    assert complete
    assert found == [str(outer)]


# --- the server side: entries follow, and downloads follow with them --------------

def test_watch_entry_updates_itself_when_the_folder_moves(monkeypatch, tmp_path):
    from bpdl.webui import server
    from bpdl.links import parse_url
    library = tmp_path / "music"
    new = _mark(library / "MAKINA" / "DNZ_RECORDS")
    entry = {"url": "https://www.beatport.com/label/dnz/1", "name": "DNZ Records",
             "destination": str(library / "OLD" / "DNZ")}
    monkeypatch.setattr(server, "_follow_roots", lambda: [str(library)])
    monkeypatch.setattr(server.bus, "publish", lambda ev: None)

    assert server._resolve_label_destination(entry, parse_url(entry["url"])) == str(new)
    assert entry["destination"] == str(new)       # persisted, so next time is O(1)
    assert entry["follow_status"] == "moved"


def test_a_lost_folder_keeps_its_recorded_path(monkeypatch, tmp_path):
    """An unplugged drive is indistinguishable from a deleted folder. Forgetting
    the path would make a temporary absence permanent."""
    from bpdl.webui import server
    from bpdl.links import parse_url
    library = tmp_path / "music"
    library.mkdir()
    recorded = str(library / "MAKINA" / "DNZ")
    entry = {"url": "https://www.beatport.com/label/dnz/1", "name": "DNZ Records",
             "destination": recorded}
    monkeypatch.setattr(server, "_follow_roots", lambda: [str(library)])
    monkeypatch.setattr(server.bus, "publish", lambda ev: None)

    assert server._resolve_label_destination(entry, parse_url(entry["url"])) == ""
    assert entry["destination"] == recorded
    assert entry["follow_status"] == "lost"


def test_a_watched_label_downloads_into_the_folder_it_moved_to(monkeypatch, tmp_path):
    """End to end: the entry still names the old path, the marker is somewhere
    else, and the downloader is handed the new folder."""
    library = tmp_path / "music"
    new = _mark(library / "MAKINA" / "DNZ_RECORDS")
    rel = _release(1, "New One", "2026-08-01", "2026-08-01T00:00:00-06:00")
    entry = {"url": "https://www.beatport.com/label/dnz/1", "name": "DNZ Records",
             "watched_since": "2026-01-01", "destination": str(library / "OLD" / "DNZ")}
    h = _WatchHarness([rel])
    server = h.install(monkeypatch, entry, staging=str(tmp_path / "staging"))
    monkeypatch.setattr(server, "_follow_roots", lambda: [str(library)])

    server._check_watched_label(entry)
    assert h.cfg_seen.downloads_directory == str(new)
    assert h.cfg_seen.track_exists == "skip"      # add-only into the library, always


# --- downloading a label starts watching it --------------------------------------

def _full_label_download(monkeypatch, tmp_path, *, label_dir, auto=True, watched=None):
    from bpdl.webui import server
    from bpdl.config import AppConfig
    events = []
    monkeypatch.setattr(server.history, "record_label_sync", lambda **kw: None)
    monkeypatch.setattr(server, "_newest_publish_date", lambda client, lid: "2026-08-20")
    monkeypatch.setattr(server, "_client_for", lambda store: object())
    monkeypatch.setattr(server.bus, "publish", lambda ev: events.append(ev))
    cfg = AppConfig(username="u", password="p", auto_watch_labels=auto,
                    downloads_directory=str(tmp_path / "downloads"),
                    watched_labels=list(watched or []))
    monkeypatch.setattr(server.state, "cfg", cfg)
    monkeypatch.setattr(server.state, "config_path", "")
    item = {"url": "https://www.beatport.com/label/dnz/1", "type": "labels", "id": 1,
            "store": "beatport", "name": "DNZ Records", "filters": None}
    run = mock.Mock(stats=mock.Mock(failed=0, downloaded=42))
    run.last_label_dir = str(label_dir) if label_dir else ""
    server._record_full_label_download(item, run)
    return cfg, events


def test_downloading_a_whole_label_starts_watching_it(monkeypatch, tmp_path):
    from bpdl import labelfollow
    folder = tmp_path / "downloads" / "DNZ Records [2026-08-26]"
    folder.mkdir(parents=True)
    cfg, events = _full_label_download(monkeypatch, tmp_path, label_dir=folder)

    assert [w["name"] for w in cfg.watched_labels] == ["DNZ Records"]
    entry = cfg.watched_labels[0]
    assert entry["destination"] == str(folder)
    # Seeded with what was just taken: the catalogue is held, so the first sweep
    # is an incremental check, not a full re-walk of every release ever published.
    assert entry[server_watermark()] == "2026-08-20"
    assert entry["auto_watched"] is True
    # and the folder is marked, so moving it into the library keeps it followed
    assert labelfollow.anchor_matches(labelfollow.read_anchor(folder), 1, "beatport")
    assert any(e["type"] == "label_auto_watched" for e in events)


def server_watermark():
    from bpdl.webui import server
    return server._WATERMARK_KEY


def test_auto_watch_can_be_turned_off_but_the_folder_is_still_marked(monkeypatch, tmp_path):
    """The marker is not the watch. Marking regardless means a label watched by
    hand months later still knows which folder is its own."""
    from bpdl import labelfollow
    folder = tmp_path / "downloads" / "DNZ Records [2026-08-26]"
    folder.mkdir(parents=True)
    cfg, _ = _full_label_download(monkeypatch, tmp_path, label_dir=folder, auto=False)
    assert cfg.watched_labels == []
    assert labelfollow.read_anchor(folder) is not None


def test_auto_watch_does_not_duplicate_an_existing_entry(monkeypatch, tmp_path):
    folder = tmp_path / "downloads" / "DNZ Records [2026-08-26]"
    folder.mkdir(parents=True)
    existing = {"url": "https://www.beatport.com/label/dnz/1", "name": "DNZ Records",
                "watched_since": "2026-01-01", "watch_from": "2020-01-01"}
    cfg, _ = _full_label_download(monkeypatch, tmp_path, label_dir=folder,
                                  watched=[existing])
    assert len(cfg.watched_labels) == 1
    assert cfg.watched_labels[0]["watch_from"] == "2020-01-01"   # left alone


def test_the_shared_downloads_folder_is_never_marked_as_a_label(monkeypatch, tmp_path):
    """With sort_by_context off, a label lands loose in the downloads directory.
    Marking that would make every unrelated download follow the label around."""
    from bpdl import labelfollow
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    cfg, _ = _full_label_download(monkeypatch, tmp_path, label_dir=downloads)
    assert labelfollow.read_anchor(downloads) is None
    # still watched — just from staging, with no folder of its own to follow
    assert cfg.watched_labels[0].get("destination") is None


def test_a_label_watched_before_it_was_downloaded_adopts_the_folder(monkeypatch, tmp_path):
    """The gap this closes: watching a label first and downloading it later left the
    entry with no folder, so it filed to staging forever even though the download
    had just landed somewhere and marked it."""
    from bpdl import labelfollow
    folder = tmp_path / "downloads" / "Baby's Back [2026-08-27]"
    folder.mkdir(parents=True)
    watched = {"url": "https://www.beatport.com/label/dnz/1", "name": "DNZ Records",
               "watched_since": "2026-01-01"}                     # no destination
    cfg, events = _full_label_download(monkeypatch, tmp_path, label_dir=folder,
                                       watched=[watched])
    assert len(cfg.watched_labels) == 1                            # not duplicated
    assert cfg.watched_labels[0]["destination"] == str(folder)
    assert labelfollow.anchor_matches(labelfollow.read_anchor(folder), 1, "beatport")
    assert any(e["type"] == "label_followed" for e in events)


def test_a_folder_the_user_chose_is_never_overwritten_by_a_download(monkeypatch, tmp_path):
    """Adopting a folder fills a blank; it must not move a label the user has
    deliberately pointed at a library folder to wherever the last download landed."""
    chosen = tmp_path / "music" / "MAKINA" / "DNZ"
    chosen.mkdir(parents=True)
    folder = tmp_path / "downloads" / "DNZ Records [2026-08-27]"
    folder.mkdir(parents=True)
    watched = {"url": "https://www.beatport.com/label/dnz/1", "name": "DNZ Records",
               "watched_since": "2026-01-01", "destination": str(chosen)}
    cfg, _ = _full_label_download(monkeypatch, tmp_path, label_dir=folder,
                                  watched=[watched])
    assert cfg.watched_labels[0]["destination"] == str(chosen)
