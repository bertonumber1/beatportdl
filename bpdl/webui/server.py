from __future__ import annotations

import asyncio
import json
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bpdl import config as config_module
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
from bpdl.scanner import rank_map, scan_artist, scan_label
from bpdl.search import extract_store_tag

STATIC_DIR = Path(__file__).parent / "static"
VERSION = "2.0.0"

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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _load_config()
    if _configured():
        threading.Thread(target=_login_background, daemon=True).start()
    yield


app = FastAPI(title="BP-DL Web", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


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


@app.get("/api/settings")
def get_settings() -> dict:
    return _cfg_dict(state.cfg)


@app.post("/api/settings")
def save_settings(payload: SettingsPayload) -> dict:
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(state.cfg, k, v)
    if not state.config_path:
        _, _ = paths.find_config_file()
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

    if raw.startswith("https://www.beatport.com") or raw.startswith("https://www.beatsource.com"):
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
        bus.publish({"type": "queue_updated", "queue": state.queue})
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
            results.append({"kind": "track", "name": name, "url": t.url, "subtitle": artists, "cover": cover})
        for r in search_data["releases"][:15]:
            artists = ", ".join(a.get("name", "") for a in r.artists[:3])
            cover = r.image.formatted_url("300x300") if r.image.dynamic_uri else ""
            results.append({"kind": "release", "name": r.name, "url": r.url, "subtitle": f"{artists} [{r.label.name}]", "cover": cover})
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
    bus.publish({"type": "queue_updated", "queue": state.queue})
    return {"item": state.queue[index]}


@app.delete("/api/queue/{index}")
def remove_from_queue(index: int) -> dict:
    if 0 <= index < len(state.queue):
        state.queue.pop(index)
        bus.publish({"type": "queue_updated", "queue": state.queue})
    return {"queue": state.queue}


@app.post("/api/queue/clear")
def clear_queue() -> dict:
    state.queue.clear()
    bus.publish({"type": "queue_updated", "queue": state.queue})
    return {"queue": state.queue}


# ---- scan / wizard -----------------------------------------------------------------

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
    items = list(state.queue)
    bus.publish({"type": "batch_start", "count": len(items)})

    total_downloaded = total_skipped = total_failed = 0
    for item in items:
        bus.publish({"type": "item_start", "url": item["url"], "name": item["name"], "cover": item.get("cover", "")})
        _apply_filters(item.get("filters"))

        run = App(state.cfg, state.bp, state.bs, on_event=bus.publish)
        try:
            run.handle_url(item["url"])
        finally:
            run.shutdown()

        total_downloaded += run.stats.downloaded
        total_skipped += sum(run.stats.skipped.values())
        total_failed += run.stats.failed
        bus.publish({"type": "item_done", "url": item["url"]})

    state.queue.clear()
    state.downloading = False
    bus.publish({
        "type": "batch_done",
        "downloaded": total_downloaded,
        "skipped": total_skipped,
        "failed": total_failed,
    })


@app.post("/api/download/start")
def start_download() -> dict:
    _require_login()
    if state.downloading:
        raise HTTPException(400, "a download is already running")
    if not state.queue:
        raise HTTPException(400, "queue is empty")
    threading.Thread(target=_run_download, daemon=True).start()
    return {"started": True}


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
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
