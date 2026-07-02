from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bpdl.auth import AUTH_ENDPOINT, LOGIN_ENDPOINT, TOKEN_ENDPOINT, Auth
from bpdl.models import Artist, Chart, Label, Playlist, Release, Track

BEATPORT_BASE_URL = "https://api.beatport.com/v4"
BEATSOURCE_BASE_URL = "https://api.beatsource.com/v4"

_NO_AUTH_CHECK_ENDPOINTS = (TOKEN_ENDPOINT, AUTH_ENDPOINT, LOGIN_ENDPOINT)

_DEFAULT_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
}


class ApiError(RuntimeError):
    pass


class Paginated:
    def __init__(self, data: dict, item_cls, store: str):
        self.next = data.get("next")
        self.count = data.get("count", 0)
        results_key = "results"
        self.results = [
            item_cls.from_json(item, store) if item_cls in (Track, Release, Label) else item_cls.from_json(item)
            for item in data.get(results_key, [])
        ]


class BeatportClient:
    def __init__(self, store: str, proxy: str, auth: Auth):
        self.store = store
        self.auth = auth
        self.headers = dict(_DEFAULT_HEADERS)
        self.base_url = BEATSOURCE_BASE_URL if store == "beatsource" else BEATPORT_BASE_URL

        self.session = requests.Session()
        retry = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def raw_fetch(
        self,
        method: str,
        endpoint: str,
        payload: dict | None = None,
        content_type: str = "",
        allow_redirects: bool = True,
    ) -> requests.Response:
        if endpoint not in _NO_AUTH_CHECK_ENDPOINTS:
            self.auth.check(self)

        headers = dict(self.headers)
        if self.auth.token and self.auth.token.access_token:
            headers["Authorization"] = f"Bearer {self.auth.token.access_token}"

        kwargs: dict[str, Any] = {"headers": headers, "timeout": 40, "allow_redirects": allow_redirects}
        if payload is not None:
            headers["Content-Type"] = content_type
            if content_type == "application/json":
                kwargs["json"] = payload
            elif content_type == "application/x-www-form-urlencoded":
                kwargs["data"] = payload
            else:
                raise ApiError(f"unsupported content type: {content_type}")

        url = self.base_url + endpoint
        try:
            resp = self.session.request(method, url, **kwargs)
        except requests.RequestException as e:
            raise ApiError(f"request failed: {e}") from e

        if resp.status_code not in (200, 302):
            if resp.status_code == 401 and endpoint not in _NO_AUTH_CHECK_ENDPOINTS:
                self.auth.invalidate()
                return self.raw_fetch(method, endpoint, payload, content_type, allow_redirects)
            detail = "Unknown error"
            try:
                body = resp.json()
                detail = body.get("detail") or body.get("error") or detail
            except ValueError:
                pass
            raise ApiError(f"request failed with status code: {resp.status_code} - {detail}")

        return resp

    def _get(self, endpoint: str) -> dict:
        return self.raw_fetch("GET", endpoint).json()

    def _paginated(self, endpoint: str, item_cls) -> Paginated:
        return Paginated(self._get(endpoint), item_cls, self.store)

    # --- tracks -----------------------------------------------------------

    def get_track(self, track_id: int) -> Track:
        return Track.from_json(self._get(f"/catalog/tracks/{track_id}/"), self.store)

    def download_track(self, track_id: int, quality: str) -> dict:
        return self._get(f"/catalog/tracks/{track_id}/download/?quality={quote(quality)}")

    # --- releases -----------------------------------------------------------

    def get_release(self, release_id: int) -> Release:
        return Release.from_json(self._get(f"/catalog/releases/{release_id}/"), self.store)

    def get_release_tracks(self, release_id: int, page: int, params: str = "") -> Paginated:
        return self._paginated(f"/catalog/releases/{release_id}/tracks/?page={page}&{params}", Track)

    # --- labels -----------------------------------------------------------

    def get_label(self, label_id: int) -> Label:
        return Label.from_json(self._get(f"/catalog/labels/{label_id}/"), self.store)

    def search_labels(self, query: str) -> Paginated:
        return self._paginated(f"/catalog/labels/?q={quote(query)}&order_by=name&per_page=10", Label)

    def get_label_releases(self, label_id: int, page: int, params: str = "") -> Paginated:
        return self._paginated(f"/catalog/labels/{label_id}/releases/?page={page}&{params}", Release)

    # --- artists -----------------------------------------------------------

    def get_artist(self, artist_id: int) -> Artist:
        return Artist.from_json(self._get(f"/catalog/artists/{artist_id}/"))

    def get_artist_tracks(self, artist_id: int, page: int, params: str = "") -> Paginated:
        return self._paginated(f"/catalog/artists/{artist_id}/tracks/?page={page}&{params}", Track)

    # --- playlists -----------------------------------------------------------

    def get_playlist(self, playlist_id: int) -> Playlist:
        return Playlist.from_json(self._get(f"/catalog/playlists/{playlist_id}/"))

    def get_playlist_items(self, playlist_id: int, page: int, params: str = "") -> Paginated:
        data = self._get(f"/catalog/playlists/{playlist_id}/tracks/?page={page}&{params}")
        pg = Paginated.__new__(Paginated)
        pg.next = data.get("next")
        pg.count = data.get("count", 0)
        pg.results = []
        for item in data.get("results", []):
            track = Track.from_json(item["track"], self.store)
            pg.results.append({"id": item.get("id", 0), "position": item.get("position", 0), "track": track})
        return pg

    # --- charts -----------------------------------------------------------

    def get_chart(self, chart_id: int) -> Chart:
        return Chart.from_json(self._get(f"/catalog/charts/{chart_id}/"))

    def get_chart_tracks(self, chart_id: int, page: int, params: str = "") -> Paginated:
        return self._paginated(f"/catalog/charts/{chart_id}/tracks/?page={page}&{params}", Track)

    # --- genres / search -----------------------------------------------------------

    def search(self, query: str) -> dict:
        data = self._get(f"/catalog/search/?q={quote(query)}&order_by=-publish_date&is_available_for_streaming=true")
        return {
            "tracks": [Track.from_json(t, self.store) for t in data.get("tracks", [])],
            "releases": [Release.from_json(r, self.store) for r in data.get("releases", [])],
        }
