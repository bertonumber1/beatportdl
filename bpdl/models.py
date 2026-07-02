from __future__ import annotations

from dataclasses import dataclass, field

from bpdl.templates import (
    duration_display,
    number_with_padding,
    parse_template,
    sanitize_for_path,
    sanitize_path,
    sanitize_string,
)

KEY_SYSTEMS = ("standard", "standard-short", "openkey", "camelot")


def _store_url(entity_id: int, entity: str, slug: str, store: str) -> str:
    domain = "beatsource.com" if store == "beatsource" else "beatport.com"
    return f"https://www.{domain}/{entity}/{slug}/{entity_id}"


@dataclass
class NamingPreferences:
    template: str
    whitespace: str = ""
    artists_limit: int = 0
    artists_short_form: str = ""
    track_number_padding: int = 0
    key_system: str = "standard-short"


def display_artists(artists: list[dict], limit: int = 0, short_form: str = "") -> str:
    if short_form and limit and len(artists) > limit:
        return short_form
    return ", ".join(a["name"] for a in artists)


@dataclass
class Key:
    name: str = ""
    letter: str = ""
    chord_type: str = ""
    camelot_number: int = 0
    camelot_letter: str = ""
    is_flat: bool = False
    is_sharp: bool = False

    @classmethod
    def from_json(cls, data: dict | None) -> "Key":
        if not data:
            return cls()
        return cls(
            name=data.get("name", ""),
            letter=data.get("letter", ""),
            chord_type=(data.get("chord_type") or {}).get("name", ""),
            camelot_number=data.get("camelot_number") or 0,
            camelot_letter=data.get("camelot_letter", ""),
            is_flat=data.get("is_flat") or False,
            is_sharp=data.get("is_sharp") or False,
        )

    def display(self, system: str) -> str:
        if system == "standard":
            return self.name
        if system == "standard-short":
            symbol = "#" if self.is_sharp else ("b" if self.is_flat else "")
            chord = "m" if self.chord_type == "Minor" else ""
            return f"{self.letter}{symbol}{chord}"
        if system == "openkey":
            number = self.camelot_number - 7 if self.camelot_number > 7 else self.camelot_number + 5
            letter = "m" if self.chord_type == "Minor" else ("d" if self.chord_type == "Major" else "")
            return f"{number}{letter}"
        if system == "camelot":
            return f"{self.camelot_number}{self.camelot_letter}"
        return ""


@dataclass
class Image:
    dynamic_uri: str = ""

    @classmethod
    def from_json(cls, data: dict | None) -> "Image":
        if not data:
            return cls()
        return cls(dynamic_uri=data.get("dynamic_uri", ""))

    def formatted_url(self, size: str) -> str:
        return self.dynamic_uri.replace("{w}x{h}", size)


@dataclass
class Genre:
    id: int = 0
    name: str = ""
    slug: str = ""

    @classmethod
    def from_json(cls, data: dict | None) -> "Genre | None":
        if not data:
            return None
        return cls(id=data.get("id") or 0, name=data.get("name", ""), slug=data.get("slug", ""))


@dataclass
class Label:
    id: int = 0
    name: str = ""
    slug: str = ""
    created: str = ""
    updated: str = ""
    store: str = "beatport"

    @classmethod
    def from_json(cls, data: dict, store: str) -> "Label":
        return cls(
            id=data.get("id") or 0,
            name=data.get("name", ""),
            slug=data.get("slug", ""),
            created=(data.get("created") or "")[:10],
            updated=(data.get("updated") or "")[:10],
            store=store,
        )

    def store_url(self) -> str:
        return _store_url(self.id, "label", self.slug, self.store)

    def directory_name(self, n: NamingPreferences) -> str:
        values = {
            "id": str(self.id),
            "name": sanitize_for_path(self.name),
            "slug": self.slug,
            "created_date": self.created,
            "updated_date": self.updated,
        }
        return sanitize_path(parse_template(n.template, values), n.whitespace)


@dataclass
class Artist:
    id: int = 0
    name: str = ""
    slug: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "Artist":
        return cls(id=data.get("id") or 0, name=data.get("name", ""), slug=data.get("slug", ""))

    def directory_name(self, n: NamingPreferences) -> str:
        values = {"id": str(self.id), "name": sanitize_for_path(self.name), "slug": self.slug}
        return sanitize_path(parse_template(n.template, values), n.whitespace)


@dataclass
class Release:
    id: int = 0
    name: str = ""
    slug: str = ""
    artists: list[dict] = field(default_factory=list)
    remixers: list[dict] = field(default_factory=list)
    catalog_number: str = ""
    upc: str = ""
    label: Label = field(default_factory=Label)
    date: str = ""
    image: Image = field(default_factory=Image)
    bpm_min: int = 0
    bpm_max: int = 0
    track_urls: list[str] = field(default_factory=list)
    track_count: int = 0
    url: str = ""
    store: str = "beatport"

    @classmethod
    def from_json(cls, data: dict, store: str) -> "Release":
        bpm_range = data.get("bpm_range") or {}
        return cls(
            id=data.get("id") or 0,
            name=sanitize_string(data.get("name") or ""),
            slug=data.get("slug", ""),
            artists=data.get("artists") or [],
            remixers=data.get("remixers") or [],
            catalog_number=sanitize_string(data.get("catalog_number") or ""),
            upc=data.get("upc", ""),
            label=Label.from_json(data.get("label") or {}, store),
            date=data.get("new_release_date", ""),
            image=Image.from_json(data.get("image")),
            bpm_min=bpm_range.get("min") or 0,
            bpm_max=bpm_range.get("max") or 0,
            track_urls=data.get("tracks") or [],
            track_count=data.get("track_count") or 0,
            url=data.get("url", ""),
            store=store,
        )

    def store_url(self) -> str:
        return _store_url(self.id, "release", self.slug, self.store)

    def year(self) -> str:
        return self.date[:4] if len(self.date) >= 4 else ""

    def directory_name(self, n: NamingPreferences) -> str:
        values = {
            "id": str(self.id),
            "name": sanitize_for_path(self.name),
            "slug": self.slug,
            "artists": sanitize_for_path(display_artists(self.artists, n.artists_limit, n.artists_short_form)),
            "remixers": sanitize_for_path(display_artists(self.remixers, n.artists_limit, n.artists_short_form)),
            "date": self.date,
            "year": self.year(),
            "track_count": number_with_padding(self.track_count, self.track_count, n.track_number_padding),
            "bpm_range": f"{self.bpm_min}-{self.bpm_max}",
            "catalog_number": sanitize_for_path(self.catalog_number),
            "upc": self.upc,
            "label": sanitize_for_path(self.label.name),
        }
        return sanitize_path(parse_template(n.template, values), n.whitespace)


@dataclass
class Track:
    id: int = 0
    name: str = ""
    mix_name: str = ""
    slug: str = ""
    number: int = 0
    key: Key = field(default_factory=Key)
    bpm: int = 0
    genre: Genre = field(default_factory=Genre)
    subgenre: Genre | None = None
    isrc: str = ""
    length_ms: int = 0
    artists: list[dict] = field(default_factory=list)
    remixers: list[dict] = field(default_factory=list)
    publish_date: str = ""
    release: Release = field(default_factory=Release)
    url: str = ""
    store: str = "beatport"

    @classmethod
    def from_json(cls, data: dict, store: str) -> "Track":
        return cls(
            id=data.get("id") or 0,
            name=sanitize_string(data.get("name") or ""),
            mix_name=sanitize_string(data.get("mix_name") or ""),
            slug=data.get("slug", ""),
            number=data.get("number") or 0,
            key=Key.from_json(data.get("key")),
            bpm=data.get("bpm") or 0,
            genre=Genre.from_json(data.get("genre")) or Genre(),
            subgenre=Genre.from_json(data.get("sub_genre")),
            isrc=data.get("isrc", ""),
            length_ms=data.get("length_ms") or 0,
            artists=data.get("artists") or [],
            remixers=data.get("remixers") or [],
            publish_date=data.get("publish_date", ""),
            release=Release.from_json(data.get("release") or {}, store) if data.get("release") else Release(),
            url=data.get("url", ""),
            store=store,
        )

    def store_url(self) -> str:
        return _store_url(self.id, "track", self.slug, self.store)

    def genre_with_subgenre(self, sep: str) -> str:
        if self.subgenre:
            return f"{self.genre.name} {sep} {self.subgenre.name}"
        return self.genre.name

    def subgenre_or_genre(self) -> str:
        return self.subgenre.name if self.subgenre else self.genre.name

    def filename(self, n: NamingPreferences) -> str:
        artists_str = display_artists(self.artists, n.artists_limit, n.artists_short_form)
        remixers_str = display_artists(self.remixers, n.artists_limit, n.artists_short_form)
        values = {
            "id": str(self.id),
            "name": sanitize_for_path(self.name),
            "slug": self.slug,
            "mix_name": sanitize_for_path(self.mix_name),
            "artists": sanitize_for_path(artists_str),
            "remixers": sanitize_for_path(remixers_str),
            "number": number_with_padding(self.number, self.release.track_count, n.track_number_padding),
            "length": duration_display(self.length_ms),
            "key": self.key.display(n.key_system),
            "bpm": str(self.bpm),
            "genre": sanitize_for_path(self.genre.name),
            "subgenre": sanitize_for_path(self.subgenre.name if self.subgenre else ""),
            "genre_with_subgenre": sanitize_for_path(self.genre_with_subgenre("-")),
            "subgenre_or_genre": sanitize_for_path(self.subgenre_or_genre()),
            "isrc": self.isrc,
            "label": sanitize_for_path(self.release.label.name),
        }
        return sanitize_path(parse_template(n.template, values), n.whitespace)


@dataclass
class Playlist:
    id: int = 0
    name: str = ""
    genres: list[str] = field(default_factory=list)
    track_count: int = 0
    bpm_range: list[int | None] = field(default_factory=list)
    length_ms: int = 0
    created_date: str = ""
    updated_date: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "Playlist":
        return cls(
            id=data.get("id") or 0,
            name=data.get("name", ""),
            genres=data.get("genres") or [],
            track_count=data.get("track_count") or 0,
            bpm_range=data.get("bpm_range") or [],
            length_ms=data.get("length_ms") or 0,
            created_date=(data.get("created_date") or "")[:10],
            updated_date=(data.get("updated_date") or "")[:10],
        )

    def directory_name(self, n: NamingPreferences) -> str:
        first_genre = self.genres[0] if self.genres else ""
        bpm_range = ""
        if len(self.bpm_range) >= 2 and self.bpm_range[0] is not None and self.bpm_range[1] is not None:
            bpm_range = f"{self.bpm_range[0]}-{self.bpm_range[1]}"
        values = {
            "id": str(self.id),
            "name": sanitize_for_path(self.name),
            "first_genre": sanitize_for_path(first_genre),
            "track_count": number_with_padding(self.track_count, self.track_count, n.track_number_padding),
            "bpm_range": bpm_range,
            "length": duration_display(self.length_ms),
            "created_date": self.created_date,
            "updated_date": self.updated_date,
        }
        return sanitize_path(parse_template(n.template, values), n.whitespace)


@dataclass
class Chart:
    id: int = 0
    name: str = ""
    slug: str = ""
    track_count: int = 0
    owner_name: str = ""
    genres: list[dict] = field(default_factory=list)
    add_date: str = ""
    change_date: str = ""
    publish_date: str = ""
    image: Image = field(default_factory=Image)

    @classmethod
    def from_json(cls, data: dict) -> "Chart":
        person = data.get("person") or {}
        return cls(
            id=data.get("id") or 0,
            name=data.get("name", ""),
            slug=data.get("slug", ""),
            track_count=data.get("track_count") or 0,
            owner_name=person.get("owner_name", ""),
            genres=data.get("genres") or [],
            add_date=(data.get("add_date") or "")[:10],
            change_date=(data.get("change_date") or "")[:10],
            publish_date=(data.get("publish_date") or "")[:10],
            image=Image.from_json(data.get("image")),
        )

    def directory_name(self, n: NamingPreferences) -> str:
        first_genre = self.genres[0]["name"] if self.genres else ""
        values = {
            "id": str(self.id),
            "name": sanitize_for_path(self.name),
            "slug": self.slug,
            "first_genre": sanitize_for_path(first_genre),
            "track_count": number_with_padding(self.track_count, self.track_count, n.track_number_padding),
            "creator": sanitize_for_path(self.owner_name),
            "created_date": self.add_date,
            "published_date": self.publish_date,
            "updated_date": self.change_date,
        }
        return sanitize_path(parse_template(n.template, values), n.whitespace)
