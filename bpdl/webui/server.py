from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bpdl import config as config_module
from bpdl import history
from bpdl import notify
from bpdl import paths
from bpdl.api import BeatportClient
from bpdl.artcheck import recheck_art
from bpdl.auth import Auth
from bpdl.events import EventBus
from bpdl.handlers import App
from bpdl.links import (
    ARTIST_LINK,
    CHART_LINK,
    LABEL_LINK,
    PLAYLIST_LINK,
    RELEASE_LINK,
    TRACK_LINK,
    parse_url,
)
from bpdl.scanner import for_paginated, rank_map, sanitize_params, scan_artist, scan_label
from bpdl.search import extract_store_tag

STATIC_DIR = Path(__file__).parent / "static"
VERSION = "2.4.1"

bus = EventBus()


class State:
    def __init__(self) -> None:
        self.cfg: config_module.AppConfig = config_module.AppConfig()
        self.config_path: Path | None = None
        self.bp: BeatportClient | None = None
        self.bs: BeatportClient | None = None
        self.login_status: str = "pending"  # pending, connecting, ok, error
        self.login_error: str = ""
        self.queue: list[dict] = []
        self.downloading: bool = False
        self.watch_checking: bool = False
        self.current_run: App | None = None
        self.stop_requested: bool = False


state = State()


def _configured() -> bool:
    return bool(state.cfg.username and state.cfg.password and state.cfg.downloads_directory)


def _client_for(store: str) -> BeatportClient:
    return state.bs if store == "beatsource" else state.bp


def _load_config() -> None:
    config_path, exists = paths.find_config_file()
    state.config_path = config_path
    if exists:
        try:
            state.cfg = config_module.parse(config_path)
        except config_module.ConfigError:
            state.cfg = config_module.AppConfig()
    else:
        state.cfg = config_module.AppConfig()


def _login_background() -> None:
    state.login_status = "connecting"
    state.login_error = ""
    bus.publish({"type": "login_status", "status": "connecting"})
    try:
        cache_path, _ = paths.find_cache_file()
        auth = Auth(state.cfg.username, state.cfg.password, cache_path)
        bp = BeatportClient("beatport", state.cfg.proxy, auth)
        bs = BeatportClient("beatsource", state.cfg.proxy, auth)
        if not auth.load_cache():
            auth.init(bp)
        state.bp, state.bs = bp, bs
        state.login_status = "ok"
        bus.publish({"type": "login_status", "status": "ok"})
    except Exception as e:
        state.bp = state.bs = None
        state.login_status = "error"
        state.login_error = str(e)
        bus.publish({"type": "login_status", "status": "error", "error": str(e)})


def _queue_file() -> Path | None:
    return state.config_path.parent / "bpdl-queue.json" if state.config_path else None


def _publish_queue() -> None:
    """Persist the queue to disk (so it survives restarts) and notify the UI."""
    qf = _queue_file()
    if qf:
        try:
            qf.write_text(json.dumps(state.queue), encoding="utf-8")
        except OSError:
            pass
    bus.publish({"type": "queue_updated", "queue": state.queue})


def _restore_queue() -> None:
    qf = _queue_file()
    if qf and qf.exists():
        try:
            items = json.loads(qf.read_text(encoding="utf-8"))
            if isinstance(items, list):
                state.queue = items
        except (OSError, ValueError):
            pass


def _init_history_background() -> None:
    try:
        history.init_db()
        if state.cfg.downloads_directory:
            result = history.backfill_from_disk(state.cfg.downloads_directory)
            bus.publish({"type": "history_backfill_done", **result})
    except Exception as e:
        bus.publish({"type": "history_backfill_error", "error": str(e)})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _load_config()
    _restore_queue()
    threading.Thread(target=_init_history_background, daemon=True).start()
    if _configured():
        threading.Thread(target=_login_background, daemon=True).start()
    threading.Thread(target=_watch_scheduler_loop, daemon=True).start()
    yield


app = FastAPI(title="BP-DL Web", lifespan=lifespan)
# Browser extensions (the bp-dl grabber) call the API cross-origin; Brave in
# particular still sends CORS preflights for extension fetches, so OPTIONS must
# succeed. Local trusted server, no cookies/credentials involved → allow any origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


from fastapi.responses import HTMLResponse

# Cache-bust static assets per deploy: browsers happily reuse a stale cached
# app.js/style.css across container rebuilds otherwise (assets have no version
# in their URLs). Stamp the asset URLs with the files' mtime at startup.
_ASSET_STAMP = str(int(max(
    (STATIC_DIR / "app.js").stat().st_mtime,
    (STATIC_DIR / "style.css").stat().st_mtime,
)))


@app.get("/")
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={_ASSET_STAMP}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={_ASSET_STAMP}"')
    return HTMLResponse(html)


# ---- status -----------------------------------------------------------------

@app.get("/api/status")
def get_status() -> dict:
    return {
        "version": VERSION,
        "configured": _configured(),
        "login_status": state.login_status,
        "login_error": state.login_error,
        "queue": state.queue,
        "downloading": state.downloading,
    }


@app.post("/api/login/retry")
def retry_login() -> dict:
    if not _configured():
        raise HTTPException(400, "account settings are not complete yet")
    if state.login_status == "connecting":
        raise HTTPException(400, "already connecting")
    threading.Thread(target=_login_background, daemon=True).start()
    return {"started": True}


# ---- settings -----------------------------------------------------------------

def _cfg_dict(cfg: config_module.AppConfig) -> dict:
    return {
        "username": cfg.username,
        "password": cfg.password,
        "quality": cfg.quality,
        "downloads_directory": cfg.downloads_directory,
        "max_global_workers": cfg.max_global_workers,
        "max_download_workers": cfg.max_download_workers,
        "sort_by_context": cfg.sort_by_context,
        "sort_by_label": cfg.sort_by_label,
        "force_release_directories": cfg.force_release_directories,
        "track_exists": cfg.track_exists,
        "track_number_padding": cfg.track_number_padding,
        "release_directory_template": cfg.release_directory_template,
        "label_directory_template": cfg.label_directory_template,
        "artist_directory_template": cfg.artist_directory_template,
        "playlist_directory_template": cfg.playlist_directory_template,
        "chart_directory_template": cfg.chart_directory_template,
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
        "watched_artists": cfg.watched_artists,
        "watch_interval_hours": cfg.watch_interval_hours,
        "notify_webhook_url": cfg.notify_webhook_url,
    }


class SettingsPayload(BaseModel):
    username: str | None = None
    password: str | None = None
    quality: str | None = None
    downloads_directory: str | None = None
    max_global_workers: int | None = None
    max_download_workers: int | None = None
    sort_by_context: bool | None = None
    sort_by_label: bool | None = None
    force_release_directories: bool | None = None
    track_exists: str | None = None
    track_number_padding: int | None = None
    release_directory_template: str | None = None
    label_directory_template: str | None = None
    artist_directory_template: str | None = None
    playlist_directory_template: str | None = None
    chart_directory_template: str | None = None
    track_file_template: str | None = None
    whitespace_character: str | None = None
    artists_limit: int | None = None
    artists_short_form: str | None = None
    key_system: str | None = None
    cover_size: str | None = None
    keep_cover: bool | None = None
    fix_tags: bool | None = None
    proxy: str | None = None
    skip_previously_downloaded: bool | None = None
    watch_interval_hours: int | None = None
    notify_webhook_url: str | None = None


@app.get("/api/settings")
def get_settings() -> dict:
    return _cfg_dict(state.cfg)


@app.post("/api/settings")
def save_settings(payload: SettingsPayload) -> dict:
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(state.cfg, k, v)
    if not state.config_path:
        state.config_path, _ = paths.find_config_file()
    config_module.save(state.cfg, state.config_path)
    bus.publish({"type": "settings_saved"})
    if ("username" in data or "password" in data) and _configured():
        state.bp = state.bs = None
        threading.Thread(target=_login_background, daemon=True).start()
    return {"ok": True, "configured": _configured()}


# ---- queue -----------------------------------------------------------------

def _require_login() -> None:
    if state.login_status != "ok" or state.bp is None or state.bs is None:
        raise HTTPException(400, "Not connected to Beatport/Beatsource yet")


def _link_metadata(link, client: BeatportClient) -> dict:
    if link.type == LABEL_LINK:
        e = client.get_label(link.id)
        return {"name": e.name, "subtitle": "Label", "cover": ""}
    if link.type == ARTIST_LINK:
        e = client.get_artist(link.id)
        return {"name": e.name, "subtitle": "Artist", "cover": ""}
    if link.type == RELEASE_LINK:
        e = client.get_release(link.id)
        artists = ", ".join(a.get("name", "") for a in e.artists)
        cover = e.image.formatted_url("300x300") if e.image.dynamic_uri else ""
        return {"name": e.name, "subtitle": artists or "Release", "cover": cover}
    if link.type == TRACK_LINK:
        e = client.get_track(link.id)
        artists = ", ".join(a.get("name", "") for a in e.artists)
        cover = e.release.image.formatted_url("300x300") if e.release.image.dynamic_uri else ""
        name = f"{e.name} ({e.mix_name})" if e.mix_name else e.name
        return {"name": name, "subtitle": artists or "Track", "cover": cover}
    if link.type == PLAYLIST_LINK:
        e = client.get_playlist(link.id)
        return {"name": e.name, "subtitle": f"Playlist · {e.track_count} tracks", "cover": ""}
    if link.type == CHART_LINK:
        e = client.get_chart(link.id)
        cover = e.image.formatted_url("300x300") if e.image.dynamic_uri else ""
        return {"name": e.name, "subtitle": f"Chart · {e.track_count} tracks", "cover": cover}
    return {"name": link.original, "subtitle": "", "cover": ""}


class QueueAddPayload(BaseModel):
    input: str


@app.post("/api/queue")
def add_to_queue(payload: QueueAddPayload) -> dict:
    _require_login()
    raw = payload.input.strip()
    if not raw:
        raise HTTPException(400, "empty input")

    # Treat any recognisable store/API URL as a link (not a search): parse_url
    # also accepts api.beatport.com/api.beatsource.com URLs, which is what the
    # catalog endpoints emit — matching only www.* used to misroute those into
    # the search branch, so cherry-picked tracks/releases silently never queued.
    if raw.startswith(("https://www.beatport.com", "https://www.beatsource.com",
                       "https://api.beatport.com", "https://api.beatsource.com")):
        try:
            link = parse_url(raw)
        except Exception as e:
            raise HTTPException(400, f"Invalid URL: {e}") from e
        client = _client_for(link.store)
        try:
            meta = _link_metadata(link, client)
        except Exception as e:
            raise HTTPException(400, f"Failed to fetch: {e}") from e
        item = {
            "url": raw,
            "type": link.type,
            "id": link.id,
            "store": link.store,
            "needs_wizard": link.type in (LABEL_LINK, ARTIST_LINK),
            "filters": None,
            **meta,
        }
        state.queue.append(item)
        _publish_queue()
        return {"added": item}

    store_tag, trimmed = extract_store_tag(raw)
    client = state.bs if store_tag == "beatsource" else state.bp
    results: list[dict] = []
    try:
        label_results = client.search_labels(trimmed)
        for lbl in label_results.results[:10]:
            results.append({"kind": "label", "name": lbl.name, "url": lbl.store_url(), "subtitle": "Label", "cover": ""})
    except Exception:
        pass
    try:
        search_data = client.search(trimmed)
        for t in search_data["tracks"][:15]:
            artists = ", ".join(a.get("name", "") for a in t.artists[:3])
            cover = t.release.image.formatted_url("300x300") if t.release.image.dynamic_uri else ""
            name = f"{t.name} ({t.mix_name})" if t.mix_name else t.name
            results.append({"kind": "track", "name": name, "url": t.store_url(), "subtitle": artists, "cover": cover, "preview": t.sample_url})
        for r in search_data["releases"][:15]:
            artists = ", ".join(a.get("name", "") for a in r.artists[:3])
            cover = r.image.formatted_url("300x300") if r.image.dynamic_uri else ""
            results.append({"kind": "release", "name": r.name, "url": r.store_url(), "subtitle": f"{artists} [{r.label.name}]", "cover": cover})
    except Exception:
        pass
    return {"search_results": results}


class FiltersPayload(BaseModel):
    genres: list[str] = []
    subgenres: list[str] = []
    artists: list[str] = []
    date_from: str = ""
    date_to: str = ""
    bypass: bool = False


@app.post("/api/queue/{index}/filters")
def set_filters(index: int, payload: FiltersPayload) -> dict:
    if index < 0 or index >= len(state.queue):
        raise HTTPException(404, "no such queue item")
    if payload.bypass:
        state.queue[index]["filters"] = None
    else:
        state.queue[index]["filters"] = payload.model_dump(exclude={"bypass"})
    state.queue[index]["needs_wizard"] = False
    _publish_queue()
    return {"item": state.queue[index]}


@app.delete("/api/queue/{index}")
def remove_from_queue(index: int) -> dict:
    if 0 <= index < len(state.queue):
        state.queue.pop(index)
        _publish_queue()
    return {"queue": state.queue}


@app.post("/api/queue/clear")
def clear_queue() -> dict:
    state.queue.clear()
    _publish_queue()
    return {"queue": state.queue}


# ---- scan / wizard -----------------------------------------------------------------

class PeekPayload(BaseModel):
    url: str


@app.post("/api/peek")
def peek(payload: PeekPayload) -> dict:
    """Cheap size check — a single API call (page 1) gives the true total count
    via Paginated.count, with no need to walk every page. Used to warn before an
    unfiltered 'queue everything' download commits to something huge (a real
    incident: 'Cherry Red Records' turned out to have 4940 releases)."""
    _require_login()
    try:
        link = parse_url(payload.url)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    client = _client_for(link.store)
    params = sanitize_params(link.params)
    try:
        if link.type == LABEL_LINK:
            page = client.get_label_releases(link.id, 1, params)
            return {"count": page.count, "kind": "releases"}
        if link.type == ARTIST_LINK:
            page = client.get_artist_tracks(link.id, 1, params)
            return {"count": page.count, "kind": "tracks"}
    except Exception as e:
        raise HTTPException(400, f"Failed to check size: {e}") from e
    return {"count": None, "kind": None}


class BrowsePayload(BaseModel):
    url: str
    page: int = 1


@app.post("/api/browse")
def browse(payload: BrowsePayload) -> dict:
    """List a label's releases (or an artist's tracks) one page at a time, so the
    user can eyeball what's there and cherry-pick — no full-catalogue scan. Each
    page is a single Beatport API call (fast), unlike /api/scan which walks every
    release's tracks to build filter facets."""
    from bpdl.models import display_artists

    _require_login()
    try:
        link = parse_url(payload.url)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    if link.type not in (LABEL_LINK, ARTIST_LINK):
        raise HTTPException(400, "browse only supports label/artist URLs")
    client = _client_for(link.store)
    params = sanitize_params(link.params)
    page = max(1, payload.page)
    try:
        if link.type == LABEL_LINK:
            pg = client.get_label_releases(link.id, page, params)
            items = [{
                "name": r.name,
                "artist": display_artists(r.artists, 3),
                "catno": r.catalog_number,
                "date": r.date,
                "year": r.year(),
                "track_count": r.track_count,
                "url": r.store_url(),
                "cover": r.image.formatted_url("150x150") if r.image.dynamic_uri else "",
            } for r in pg.results]
            kind = "releases"
        else:
            pg = client.get_artist_tracks(link.id, page, params)
            items = [{
                "name": f"{t.name} ({t.mix_name})" if t.mix_name else t.name,
                "artist": display_artists(t.artists, 3),
                "catno": t.release.catalog_number if t.release else "",
                "date": t.publish_date,
                "year": t.publish_date[:4] if len(t.publish_date) >= 4 else "",
                "track_count": 0,
                "url": t.store_url(),
                "cover": t.release.image.formatted_url("150x150") if (t.release and t.release.image.dynamic_uri) else "",
            } for t in pg.results]
            kind = "tracks"
    except Exception as e:
        raise HTTPException(400, f"Failed to browse: {e}") from e
    return {
        "items": items,
        "page": page,
        "count": pg.count or 0,
        "has_next": bool(pg.next),
        "has_prev": page > 1,
        "kind": kind,
    }


@app.get("/api/genres")
def genres() -> dict:
    """Beatport's top-level genre list — populates the filter dropdown. Cheap,
    one API call; genres are store-wide (not label-specific)."""
    _require_login()
    try:
        data = state.bp._get("/catalog/genres/?per_page=200")
    except Exception as e:
        raise HTTPException(400, f"Failed to load genres: {e}") from e
    gl = sorted(({"id": g.get("id"), "name": g.get("name")} for g in data.get("results", [])), key=lambda x: (x["name"] or "").lower())
    return {"genres": gl}


@app.get("/api/subgenres/{genre_id}")
def subgenres(genre_id: int) -> dict:
    _require_login()
    try:
        data = state.bp._get(f"/catalog/genres/{genre_id}/sub-genres/?per_page=200")
    except Exception as e:
        raise HTTPException(400, f"Failed to load sub-genres: {e}") from e
    sl = sorted(({"id": s.get("id"), "name": s.get("name")} for s in data.get("results", [])), key=lambda x: (x["name"] or "").lower())
    return {"subgenres": sl}


class FilterPayload(BaseModel):
    url: str
    genre_id: int | None = None
    sub_genre_id: int | None = None
    bpm_min: int | None = None
    bpm_max: int | None = None
    artist_ids: list[int] = []
    order_by: str = "-publish_date"
    page: int = 1
    want_facet: bool = False


_FACET_CAP_PAGES = 5   # ≤500 filtered tracks scanned to build the artist tick-list


def _track_item(t) -> dict:
    """Uniform track payload for any track grid (filter shortlist, explore lists)."""
    from bpdl.models import display_artists

    return {
        "id": t.id,
        "name": f"{t.name} ({t.mix_name})" if t.mix_name else t.name,
        "artist": display_artists(t.artists, 3),
        "bpm": t.bpm,
        "genre": t.genre.name if t.genre else "",
        "key": t.key.name if getattr(t, "key", None) else "",
        "length": f"{t.length_ms // 60000}:{(t.length_ms // 1000) % 60:02d}" if t.length_ms else "",
        "year": t.publish_date[:4] if len(t.publish_date or "") >= 4 else "",
        "url": t.store_url(),
        "cover": t.release.image.formatted_url("150x150") if (t.release and t.release.image.dynamic_uri) else "",
        "preview": t.sample_url,
        "artists": [{"id": a.get("id"), "name": a.get("name", ""), "slug": a.get("slug", "")}
                    for a in (t.artists or [])],
        "label": ({"id": t.release.label.id, "name": t.release.label.name, "slug": t.release.label.slug}
                  if (t.release and t.release.label and t.release.label.id) else None),
    }


@app.post("/api/filter")
def filter_tracks(payload: FilterPayload) -> dict:
    """Server-side faceted filtering, the way Beatport's own site does it: filter a
    label's (or artist's) TRACKS by bpm range + genre + sub-genre + artist(s), all
    applied by Beatport — we just page the already-narrowed result. `want_facet`
    additionally returns the distinct-artist tick-list across the filtered set."""
    from urllib.parse import quote
    from bpdl.models import Track, display_artists

    _require_login()
    try:
        link = parse_url(payload.url)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    if link.type not in (LABEL_LINK, ARTIST_LINK):
        raise HTTPException(400, "filter only supports label/artist URLs")

    base = [f"label_id={link.id}"] if link.type == LABEL_LINK else [f"artist_id={link.id}"]
    if payload.genre_id:
        base.append(f"genre_id={payload.genre_id}")
    if payload.sub_genre_id:
        base.append(f"sub_genre_id={payload.sub_genre_id}")
    if payload.bpm_min and payload.bpm_max:
        base.append(f"bpm={payload.bpm_min}:{payload.bpm_max}")
    if payload.artist_ids and link.type == LABEL_LINK:
        base.append("artist_id=" + ",".join(str(a) for a in payload.artist_ids))
    base.append(f"order_by={quote(payload.order_by or '-publish_date')}")
    qs = "&".join(base)

    try:
        page = max(1, payload.page)
        pg = state.bp._paginated(f"/catalog/tracks/?{qs}&per_page=100&page={page}", Track)
        items = [_track_item(t) for t in pg.results]

        facet = None
        if payload.want_facet and link.type == LABEL_LINK:
            counts: dict[int, list] = {}
            p = 1
            while p <= _FACET_CAP_PAGES:
                fp = pg if p == page == 1 else state.bp._paginated(
                    f"/catalog/tracks/?{qs}&per_page=100&page={p}",
                    Track)
                for t in fp.results:
                    for a in (t.artists or []):
                        aid = a.get("id")
                        if aid is None:
                            continue
                        rec = counts.setdefault(aid, [a.get("name", ""), 0])
                        rec[1] += 1
                if not fp.next:
                    break
                p += 1
            facet = sorted(
                ({"id": aid, "name": v[0], "count": v[1]} for aid, v in counts.items()),
                key=lambda x: -x["count"])
    except Exception as e:
        raise HTTPException(400, f"Filter failed: {e}") from e

    return {
        "tracks": items,
        "count": pg.count or 0,
        "page": page,
        "has_next": bool(pg.next),
        "has_prev": page > 1,
        "artists": facet,
    }


class ExplorePayload(BaseModel):
    section: str = "top100"  # top100 | tracks | releases | charts
    genre_id: int | None = None
    bpm_min: int | None = None
    bpm_max: int | None = None
    page: int = 1


@app.post("/api/explore")
def explore(payload: ExplorePayload) -> dict:
    """Storefront browsing, like beatport.com's own home/genre pages: genre Top 100
    (or the overall Beatport Top 100), newest releases, and DJ charts — every item
    queueable by its store URL."""
    from bpdl.models import Chart, Release, Track, _store_url, display_artists

    _require_login()
    page = max(1, payload.page)
    g = f"genre_id={payload.genre_id}&" if payload.genre_id else ""
    try:
        if payload.section == "top100":
            path = (f"/catalog/genres/{payload.genre_id}/top/100/?per_page=100"
                    if payload.genre_id else "/catalog/tracks/top/100/?per_page=100")
            pg = state.bp._paginated(path, Track)
            return {"kind": "tracks", "items": [_track_item(t) for t in pg.results],
                    "count": pg.count or 0, "page": 1, "has_next": False, "has_prev": False}

        if payload.section == "tracks":
            bpm = (f"bpm={payload.bpm_min}:{payload.bpm_max}&"
                   if payload.bpm_min and payload.bpm_max else "")
            pg = state.bp._paginated(
                f"/catalog/tracks/?{g}{bpm}order_by=-publish_date"
                f"&publish_date=:{date.today().isoformat()}&per_page=50&page={page}", Track)
            return {"kind": "tracks", "items": [_track_item(t) for t in pg.results],
                    "count": pg.count or 0, "page": page,
                    "has_next": bool(pg.next), "has_prev": page > 1}

        if payload.section == "releases":
            # publish_date=:today (colon range, like bpm) excludes pre-orders —
            # plain -publish_date sorting puts future-dated releases first
            pg = state.bp._paginated(
                f"/catalog/releases/?{g}order_by=-publish_date"
                f"&publish_date=:{date.today().isoformat()}&per_page=50&page={page}", Release)
            items = [{
                "name": r.name,
                "artist": display_artists(r.artists, 3),
                "catno": r.catalog_number,
                "date": r.date,
                "year": r.year(),
                "track_count": r.track_count,
                "url": r.store_url(),
                "cover": r.image.formatted_url("150x150") if r.image.dynamic_uri else "",
            } for r in pg.results]
            return {"kind": "releases", "items": items, "count": pg.count or 0,
                    "page": page, "has_next": bool(pg.next), "has_prev": page > 1}

        if payload.section == "charts":
            pg = state.bp._paginated(
                f"/catalog/charts/?{g}order_by=-publish_date&per_page=50&page={page}", Chart)
            items = [{
                "name": c.name,
                "artist": c.owner_name,
                "date": c.publish_date,
                "track_count": c.track_count,
                "url": _store_url(c.id, "chart", c.slug, "beatport"),
                "cover": c.image.formatted_url("150x150") if c.image.dynamic_uri else "",
            } for c in pg.results]
            return {"kind": "charts", "items": items, "count": pg.count or 0,
                    "page": page, "has_next": bool(pg.next), "has_prev": page > 1}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Explore failed: {e}") from e
    raise HTTPException(400, f"unknown section: {payload.section}")


class ScanPayload(BaseModel):
    url: str


@app.post("/api/scan")
def start_scan(payload: ScanPayload) -> dict:
    _require_login()
    try:
        link = parse_url(payload.url)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    if link.type not in (LABEL_LINK, ARTIST_LINK):
        raise HTTPException(400, "scan only supports label/artist URLs")
    client = _client_for(link.store)

    def run() -> None:
        def progress(msg: str) -> None:
            bus.publish({"type": "scan_status", "url": payload.url, "message": msg})

        try:
            if link.type == LABEL_LINK:
                stats = scan_label(client, link, progress)
            else:
                stats = scan_artist(client, link, progress)
        except Exception as e:
            bus.publish({"type": "scan_error", "url": payload.url, "error": str(e)})
            return
        bus.publish({
            "type": "scan_done",
            "url": payload.url,
            "total": stats.total,
            "genres": [{"name": e.name, "count": e.count} for e in rank_map(stats.genres)],
            "subgenres": [{"name": e.name, "count": e.count} for e in rank_map(stats.subgenres)],
            "artists": [{"name": e.name, "count": e.count} for e in rank_map(stats.artists)[:40]],
            "bpm_min": stats.bpm_min if stats.bpm_max else 0,
            "bpm_max": stats.bpm_max,
        })

    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


# ---- download -----------------------------------------------------------------

def _apply_filters(filters: dict | None) -> None:
    if filters is None:
        state.cfg.filter_genres = []
        state.cfg.filter_subgenres = []
        state.cfg.filter_artists = []
        state.cfg.filter_publish_date_from = ""
        state.cfg.filter_publish_date_to = ""
    else:
        state.cfg.filter_genres = filters.get("genres", [])
        state.cfg.filter_subgenres = filters.get("subgenres", [])
        state.cfg.filter_artists = filters.get("artists", [])
        state.cfg.filter_publish_date_from = filters.get("date_from", "")
        state.cfg.filter_publish_date_to = filters.get("date_to", "")


def _run_download() -> None:
    state.downloading = True
    state.stop_requested = False
    bus.publish({"type": "batch_start", "count": len(state.queue)})

    total_downloaded = total_skipped = total_failed = 0
    failed_tracks: list[dict] = []
    stopped_early = False
    # Track finished items by identity rather than snapshotting the queue up
    # front: a label queued *while* this batch is running gets appended to
    # state.queue and must be picked up here, not frozen out.
    processed_ids: set[int] = set()

    def on_event(ev: dict) -> None:
        if ev.get("type") == "track_error" and ev.get("url"):
            failed_tracks.append({"url": ev["url"], "name": ev.get("name") or ev.get("id", "")})
        bus.publish(ev)

    # The whole batch runs inside try/finally: this thread is the only thing
    # that can ever reset state.downloading, so an exception escaping here
    # would otherwise leave the UI permanently reporting "a download is
    # already running" until the server restarts.
    try:
        while True:
            if state.stop_requested:
                stopped_early = True
                break
            # Re-scan the live queue each iteration for the next item we haven't
            # done yet. This is how mid-run additions get processed, and it's
            # robust to items being removed from the queue underneath us.
            # Skip items still awaiting the wizard: a label/artist added mid-run
            # keeps needs_wizard=True (and filters=None) until the user finishes
            # scoping it. Grabbing one here would _apply_filters(None) and pull
            # the ENTIRE unfiltered catalogue out from under the filter picker.
            item = next((q for q in state.queue
                         if id(q) not in processed_ids and not q.get("needs_wizard")), None)
            if item is None:
                break

            bus.publish({"type": "item_start", "url": item["url"], "name": item["name"], "cover": item.get("cover", "")})
            _apply_filters(item.get("filters"))

            run = App(state.cfg, state.bp, state.bs, on_event=on_event)
            state.current_run = run
            try:
                # handle_url() catches per-track errors itself, but setup steps
                # (e.g. mkdir on a bad/unwritable downloads directory) can still
                # raise — count it against this item and move on to the next.
                run.handle_url(item["url"])
            except Exception as e:
                run.stats.add_failed()
                bus.publish({"type": "track_error", "id": item["url"], "name": item["name"], "reason": str(e), "url": item["url"]})
                failed_tracks.append({"url": item["url"], "name": item["name"]})
            finally:
                run.shutdown(cancel_pending=state.stop_requested)
                state.current_run = None

            total_downloaded += run.stats.downloaded
            total_skipped += sum(run.stats.skipped.values())
            total_failed += run.stats.failed
            bus.publish({"type": "item_done", "url": item["url"]})
            processed_ids.add(id(item))
    finally:
        # Drop exactly the items we finished, keeping everything still pending:
        # labels queued mid-run that we hadn't reached, and — after a stop — the
        # remainder. A blanket clear here used to wipe mid-run additions.
        state.queue = [q for q in state.queue if id(q) not in processed_ids]
        # Per-item filters mutate the shared cfg — reset so a later watch-list
        # check doesn't silently inherit the last queue item's filters.
        _apply_filters(None)
        state.downloading = False
        state.stop_requested = False
        _publish_queue()
        bus.publish({
            "type": "batch_done",
            "downloaded": total_downloaded,
            "skipped": total_skipped,
            "failed": total_failed,
            "failed_tracks": failed_tracks,
            "stopped": stopped_early,
        })


@app.post("/api/download/stop")
def stop_download() -> dict:
    if not state.downloading:
        raise HTTPException(400, "nothing is downloading")
    state.stop_requested = True
    if state.current_run:
        state.current_run.cancel()
    return {"stopping": True}


@app.post("/api/download/start")
def start_download() -> dict:
    _require_login()
    if state.downloading:
        raise HTTPException(400, "a download is already running")
    if not state.queue:
        raise HTTPException(400, "queue is empty")
    if not any(not q.get("needs_wizard") for q in state.queue):
        raise HTTPException(400, "finish choosing filters (or 'queue everything') before starting")
    threading.Thread(target=_run_download, daemon=True).start()
    return {"started": True}


# ---- watch-list -----------------------------------------------------------------

class WatchAddPayload(BaseModel):
    url: str


def _watch_response() -> dict:
    labels = [{**e, "pending_releases": history.get_all_pending(e["url"])} for e in state.cfg.watched_labels]
    artists = [{**e, "pending_releases": history.get_all_pending(e["url"])} for e in state.cfg.watched_artists]
    return {
        "watched_labels": labels,
        "watched_artists": artists,
        "interval_hours": state.cfg.watch_interval_hours,
    }


@app.get("/api/watch")
def list_watch() -> dict:
    return _watch_response()


@app.post("/api/watch")
def add_watch(payload: WatchAddPayload) -> dict:
    _require_login()
    try:
        link = parse_url(payload.url)
    except Exception as e:
        raise HTTPException(400, f"Invalid URL: {e}") from e
    if link.type not in (LABEL_LINK, ARTIST_LINK):
        raise HTTPException(400, "watching only supports label or artist URLs")
    is_artist = link.type == ARTIST_LINK
    target = state.cfg.watched_artists if is_artist else state.cfg.watched_labels
    if any(w["url"] == payload.url for w in target):
        raise HTTPException(400, f"already watching this {'artist' if is_artist else 'label'}")
    client = _client_for(link.store)
    try:
        name = client.get_artist(link.id).name if is_artist else client.get_label(link.id).name
    except Exception as e:
        raise HTTPException(400, f"Failed to fetch {'artist' if is_artist else 'label'}: {e}") from e
    # watched_since anchors what counts as "new" — only releases/tracks published
    # on or after today count as genuinely new; the existing back-catalogue gets
    # baselined (marked seen, not downloaded) the first time it's checked.
    entry = {"url": payload.url, "name": name, "watched_since": datetime.now(timezone.utc).date().isoformat()}
    target.append(entry)
    config_module.save(state.cfg, state.config_path)
    return _watch_response()


@app.delete("/api/watch/{kind}/{index}")
def remove_watch(kind: str, index: int) -> dict:
    target = state.cfg.watched_artists if kind == "artist" else state.cfg.watched_labels
    if 0 <= index < len(target):
        target.pop(index)
        config_module.save(state.cfg, state.config_path)
    return _watch_response()


@app.post("/api/watch/check-now")
def watch_check_now() -> dict:
    _require_login()
    if state.watch_checking:
        raise HTTPException(400, "a watch check is already running")
    if not (state.cfg.watched_labels or state.cfg.watched_artists):
        raise HTTPException(400, "nothing is being watched")
    threading.Thread(target=_run_watch_check, daemon=True).start()
    return {"started": True}


def _parse_release_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        return None


def _check_watched_label(entry: dict) -> dict:
    try:
        link = parse_url(entry["url"])
    except Exception as e:
        return {"new_releases": 0, "new_tracks": 0, "error": str(e)}
    client = _client_for(link.store)
    label_url = entry["url"]
    watched_since = _parse_release_date(entry.get("watched_since", "")) or date.min
    today = datetime.now(timezone.utc).date()

    new_releases = []
    newly_pending = []
    try:
        def on_release(release, _i):
            if history.is_release_seen(release.id):
                return
            release_date = _parse_release_date(release.date)
            if release_date is None:
                # Can't tell how old it is — baseline it rather than guess-download.
                history.mark_release_baseline(release.id, release.name, release.label.name)
            elif release_date > today:
                # Pre-release: not downloadable yet, track it and recheck each cycle.
                history.add_pending_release(release.id, label_url, release.name, release_date.isoformat())
                newly_pending.append(release)
            elif release_date >= watched_since:
                new_releases.append(release)
            else:
                # Existing catalogue that predates when we started watching this label.
                history.mark_release_baseline(release.id, release.name, release.label.name)

        for_paginated(link.id, "", client.get_label_releases, on_release)
    except Exception as e:
        return {"new_releases": 0, "new_tracks": 0, "error": str(e)}

    # Pre-releases we were already tracking whose date has now arrived.
    due = history.get_due_pending(label_url)
    for row in due:
        try:
            release = client.get_release(row["release_id"])
            new_releases.append(release)
        except Exception:
            pass
        history.remove_pending(row["release_id"], label_url)

    total_tracks = 0
    if new_releases:
        run = App(state.cfg, state.bp, state.bs, on_event=bus.publish)
        for release in new_releases:
            try:
                run.handle_url(release.store_url())
            except Exception:
                pass
        run.shutdown()
        total_tracks = run.stats.downloaded

    return {
        "new_releases": len(new_releases),
        "new_tracks": total_tracks,
        "newly_pending": len(newly_pending),
        "names": [r.name for r in new_releases],
    }


def _check_watched_artist(entry: dict) -> dict:
    """Artist watch is track-granular: an artist can appear on a compilation we
    don't otherwise want, so we detect and grab only their individual new tracks
    rather than whole releases (that's the label watch's job). Baselining, dedup
    and pre-release tracking all mirror _check_watched_label but keyed on tracks."""
    try:
        link = parse_url(entry["url"])
    except Exception as e:
        return {"new_releases": 0, "new_tracks": 0, "error": str(e)}
    client = _client_for(link.store)
    artist_url = entry["url"]
    watched_since = _parse_release_date(entry.get("watched_since", "")) or date.min
    today = datetime.now(timezone.utc).date()

    new_tracks = []
    newly_pending = []
    try:
        def on_track(track, _i):
            if history.is_track_seen(track.id):
                return
            artists_str = ", ".join(a.get("name", "") for a in track.artists)
            rel = track.release
            track_date = _parse_release_date(track.publish_date)
            if track_date is None:
                history.mark_track_baseline(track.id, rel.id, track.name, artists_str, rel.name, rel.label.name)
            elif track_date > today:
                # Pre-release track: tracked (not baselined) so it re-evaluates each
                # cycle and downloads once its date arrives.
                history.add_pending_release(track.id, artist_url, track.name, track_date.isoformat())
                newly_pending.append(track)
            elif track_date >= watched_since:
                new_tracks.append(track)
            else:
                history.mark_track_baseline(track.id, rel.id, track.name, artists_str, rel.name, rel.label.name)

        for_paginated(link.id, "", client.get_artist_tracks, on_track)
    except Exception as e:
        return {"new_releases": 0, "new_tracks": 0, "error": str(e)}

    # Pre-release tracks we were tracking whose date has now arrived are picked up
    # by on_track above (they're never baselined); just clear them from pending.
    for row in history.get_due_pending(artist_url):
        history.remove_pending(row["release_id"], artist_url)

    total_tracks = 0
    if new_tracks:
        run = App(state.cfg, state.bp, state.bs, on_event=bus.publish)
        for track in new_tracks:
            try:
                run.handle_url(track.store_url())
            except Exception:
                pass
        run.shutdown()
        total_tracks = run.stats.downloaded

    return {
        "new_releases": len(new_tracks),
        "new_tracks": total_tracks,
        "newly_pending": len(newly_pending),
        "names": [t.name for t in new_tracks],
    }


def _run_watch_check() -> None:
    if state.watch_checking or state.downloading or state.login_status != "ok":
        return
    if not (state.cfg.watched_labels or state.cfg.watched_artists):
        return

    state.watch_checking = True
    # Watch downloads must never inherit filters left over from a queue item.
    _apply_filters(None)
    watched = (
        [(e, _check_watched_label) for e in state.cfg.watched_labels]
        + [(e, _check_watched_artist) for e in state.cfg.watched_artists]
    )
    bus.publish({"type": "watch_check_start", "count": len(watched)})
    summary_lines = []
    total_new_releases = total_new_tracks = total_pending = 0
    try:
        for entry, checker in watched:
            bus.publish({"type": "watch_check_status", "message": f"Checking {entry['name']}..."})
            result = checker(entry)
            if result.get("new_releases"):
                unit = "track" if checker is _check_watched_artist else "release"
                summary_lines.append(f"{entry['name']}: {result['new_releases']} new {unit}(s), {result['new_tracks']} track(s)")
                total_new_releases += result["new_releases"]
                total_new_tracks += result["new_tracks"]
            total_pending += result.get("newly_pending", 0)
    finally:
        state.watch_checking = False

    bus.publish({
        "type": "watch_check_done",
        "new_releases": total_new_releases,
        "new_tracks": total_new_tracks,
        "newly_pending": total_pending,
        "summary": summary_lines,
    })
    if summary_lines:
        notify.send_notification(
            state.cfg.notify_webhook_url,
            "beatportdl-webui: new releases found",
            "\n".join(summary_lines),
        )


def _watch_scheduler_loop() -> None:
    # Sleep in one-minute slices instead of one interval-long sleep, so a
    # changed watch_interval_hours setting takes effect on the next slice
    # rather than only after the previous (possibly much longer) sleep ends.
    slept = 0
    while True:
        time.sleep(60)
        slept += 60
        if slept < max(1, state.cfg.watch_interval_hours) * 3600:
            continue
        slept = 0
        try:
            _run_watch_check()
        except Exception as e:
            bus.publish({"type": "watch_check_error", "error": str(e)})


# ---- stats -----------------------------------------------------------------

@app.get("/api/stats")
def get_stats() -> dict:
    return history.get_stats()


# ---- history / library maintenance -----------------------------------------------------------------

@app.get("/api/history/verify")
def verify_library() -> dict:
    return history.verify_library()


@app.post("/api/history/remove-missing")
def remove_missing() -> dict:
    removed = history.remove_missing_entries()
    return {"removed": removed}


@app.post("/api/history/clear")
def clear_history() -> dict:
    removed = history.clear_all()
    return {"removed": removed}


# ---- library maintenance -----------------------------------------------------------------

class ArtRecheckPayload(BaseModel):
    only_missing: bool = True


@app.post("/api/art/recheck")
def start_art_recheck(payload: ArtRecheckPayload) -> dict:
    _require_login()
    if state.downloading:
        raise HTTPException(400, "wait for the current download to finish first")

    def run() -> None:
        def progress(msg: str) -> None:
            bus.publish({"type": "art_recheck_status", "message": msg})

        bus.publish({"type": "art_recheck_status", "message": "Scanning downloads directory for audio files..."})
        try:
            result = recheck_art(state.cfg, state.bp, state.bs, only_missing=payload.only_missing, on_progress=progress)
        except Exception as e:
            bus.publish({"type": "art_recheck_error", "error": str(e)})
            return
        bus.publish({"type": "art_recheck_done", **result})

    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


# ---- SSE stream -----------------------------------------------------------------

@app.get("/api/events")
async def stream_events(request: Request) -> StreamingResponse:
    q = bus.subscribe()

    async def gen():
        try:
            yield "retry: 2000\n\n"
            loop = asyncio.get_event_loop()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await loop.run_in_executor(None, q.get, True, 15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


def main() -> None:
    import os

    import uvicorn

    port = int(os.environ.get("BPDL_WEB_PORT", "8095"))
    url = f"http://localhost:{port}"
    # Prominent banner so someone launching the Windows .exe (which just opens a
    # console) knows the UI lives in a browser at this address — the app has no
    # window of its own. Printed to the same console before uvicorn's own logs.
    banner = (
        "\n"
        "  ============================================================\n"
        "     Unspok3n  ·  BP-DL  is running\n"
        "  ------------------------------------------------------------\n"
        f"     Open this address in your web browser:\n"
        f"         {url}\n"
        "\n"
        "     Keep this window open while you use the app.\n"
        "     Close this window to stop it.\n"
        "  ============================================================\n"
    )
    print(banner, flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
