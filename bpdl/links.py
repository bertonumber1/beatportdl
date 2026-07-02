from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

STORE_BEATPORT = "beatport"
STORE_BEATSOURCE = "beatsource"

TRACK_LINK = "tracks"
RELEASE_LINK = "releases"
PLAYLIST_LINK = "playlists"
CHART_LINK = "charts"
LABEL_LINK = "labels"
ARTIST_LINK = "artists"

_HOST_STORE = {
    "www.beatport.com": STORE_BEATPORT,
    "api.beatport.com": STORE_BEATPORT,
    "www.beatsource.com": STORE_BEATSOURCE,
    "api.beatsource.com": STORE_BEATSOURCE,
}


class InvalidUrlError(ValueError):
    pass


@dataclass
class Link:
    original: str
    type: str
    id: int
    params: str
    store: str


def parse_url(input_url: str) -> Link:
    u = urlparse(input_url)
    store = _HOST_STORE.get(u.netloc)
    if store is None:
        raise InvalidUrlError(f"unsupported host: {u.netloc}")

    segments = [s for s in u.path.strip("/").split("/") if s != ""]
    if not segments:
        raise InvalidUrlError("empty path")

    if len(segments) > 1 and len(segments[0]) == 2:
        segments = segments[1:]
        if segments and segments[0] == "catalog":
            segments = segments[1:]

    if not segments:
        raise InvalidUrlError("empty path after locale/catalog strip")

    id_segment = 2
    kind = segments[0]
    if kind == "track":
        link_type = TRACK_LINK
    elif kind == "release":
        link_type = RELEASE_LINK
    elif kind == "library":
        if len(segments) < 2 or segments[1] not in ("playlists", "playlist"):
            raise InvalidUrlError(f"invalid link type: {kind}/{segments[1] if len(segments) > 1 else ''}")
        link_type = PLAYLIST_LINK
    elif kind == "playlists":
        link_type = PLAYLIST_LINK
    elif kind in ("chart", "playlist"):
        link_type = CHART_LINK
    elif kind == "label":
        link_type = LABEL_LINK
    elif kind == "artist":
        link_type = ARTIST_LINK
    elif kind == "tracks":
        id_segment = 1
        link_type = TRACK_LINK
    elif kind == "releases":
        id_segment = 1
        link_type = RELEASE_LINK
    else:
        raise InvalidUrlError(f"invalid link type: {kind}")

    if id_segment + 1 > len(segments):
        raise InvalidUrlError("missing id segment")

    try:
        link_id = int(segments[id_segment])
    except ValueError as e:
        raise InvalidUrlError(f"invalid id: {segments[id_segment]}") from e

    return Link(original=input_url, type=link_type, id=link_id, params=u.query, store=store)
