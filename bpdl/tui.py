from __future__ import annotations

from dataclasses import dataclass, field

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Select, SelectionList, Static, Switch

from bpdl import config as config_module
from bpdl.links import ARTIST_LINK, LABEL_LINK, parse_url
from bpdl.scanner import RankEntry, rank_map, scan_artist, scan_label
from bpdl.search import extract_store_tag

# Clean, near-black terminal theme — one restrained accent color, thin borders,
# high-contrast text. No decorative color-mixing.
_BG = "#0a0a0a"
_SURFACE = "#141414"
_BORDER = "#333333"
_ACCENT = "#58a6ff"
_TEXT = "#e6edf3"
_MUTED = "#8b949e"
_SUCCESS = "#3fb950"
_ERROR = "#f85149"

NAUTICAL_CSS = f"""
Screen {{ background: {_BG}; }}
Header {{ background: {_SURFACE}; color: {_TEXT}; text-style: bold; border-bottom: solid {_BORDER}; }}
Footer {{ background: {_SURFACE}; color: {_MUTED}; }}
Footer > .footer-key-foreground {{ color: {_ACCENT}; text-style: bold; }}
#heading {{ padding: 1 2; text-style: bold; color: {_TEXT}; }}
SelectionList {{ height: 1fr; scrollbar-color: {_BORDER}; background: {_BG}; }}
SelectionList > .selection-list--option-highlighted {{ background: {_SURFACE}; }}
SelectionList > .selection-list--button-selected {{ color: {_ACCENT}; }}
VerticalScroll {{ padding: 1 2; height: auto; background: {_BG}; }}
Button {{ margin: 1 2; width: 100%; border: round {_BORDER}; color: {_TEXT}; background: {_SURFACE}; }}
Button:hover {{ border: round {_ACCENT}; }}
Button.-success {{ border: round {_SUCCESS}; color: {_SUCCESS}; }}
Button.-error {{ border: round {_ERROR}; color: {_ERROR}; }}
Input {{ border: round {_BORDER}; background: {_SURFACE}; color: {_TEXT}; }}
Input:focus {{ border: round {_ACCENT}; }}
Select {{ border: round {_BORDER}; background: {_SURFACE}; }}
#queue_display {{ padding: 1 2; color: {_ACCENT}; text-style: bold; }}
#status {{ padding: 0 2; color: {_MUTED}; }}
#hint {{
    margin: 1 2; padding: 1 2;
    border: round {_BORDER};
    background: {_SURFACE};
    color: {_TEXT};
}}
"""


@dataclass
class WizardResult:
    bypass: bool = False
    cancelled: bool = False
    genres: list[str] = field(default_factory=list)
    subgenres: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    date_from: str = ""
    date_to: str = ""


def _normalise_date_bound(raw: str, end_of_period: bool) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if len(raw) == 4:
        return f"{raw}-12-31" if end_of_period else f"{raw}-01-01"
    if len(raw) == 7:
        return f"{raw}-31" if end_of_period else f"{raw}-01"
    return raw


class SelectScreen(Screen):
    BINDINGS = [
        Binding("a", "select_all", "Select all"),
        Binding("n", "select_none", "Select none"),
        Binding("enter", "confirm", "Continue", priority=True),
        Binding("b", "go_back", "Back"),
        Binding("q", "bypass", "Bypass all filters"),
    ]

    def __init__(self, title: str, entries: list[RankEntry]):
        super().__init__()
        self.title_text = title
        self.entries = entries

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"{self.title_text}  (space/enter on a row to toggle, Enter to continue)", id="heading")
        options = [(f"{e.name}  —  {e.count} tracks", e.name) for e in self.entries]
        yield SelectionList[str](*options, id="list")
        yield Footer()

    def action_select_all(self) -> None:
        self.query_one(SelectionList).select_all()

    def action_select_none(self) -> None:
        self.query_one(SelectionList).deselect_all()

    def action_confirm(self) -> None:
        selected = list(self.query_one(SelectionList).selected)
        self.dismiss(("confirm", selected))

    def action_go_back(self) -> None:
        self.dismiss(("back", None))

    def action_bypass(self) -> None:
        self.dismiss(("bypass", None))


class DateScreen(Screen):
    BINDINGS = [
        Binding("enter", "confirm", "Continue", priority=True),
        Binding("b", "go_back", "Back"),
        Binding("q", "bypass", "Bypass all filters"),
    ]

    def __init__(self, prompt: str, end_of_period: bool):
        super().__init__()
        self.prompt_text = prompt
        self.end_of_period = end_of_period

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self.prompt_text, id="heading")
        yield Input(placeholder="e.g. 1996 or 1996-06-01, leave empty for no bound", id="date_input")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def action_confirm(self) -> None:
        raw = self.query_one(Input).value
        self.dismiss(("confirm", _normalise_date_bound(raw, self.end_of_period)))

    def action_go_back(self) -> None:
        self.dismiss(("back", None))

    def action_bypass(self) -> None:
        self.dismiss(("bypass", None))


class ConfirmScreen(Screen):
    BINDINGS = [
        Binding("b", "go_back", "Back"),
        Binding("q", "bypass", "Bypass all filters"),
    ]

    def __init__(self, result: WizardResult):
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        r = self.result
        date_range = "all time"
        if r.date_from and r.date_to:
            date_range = f"{r.date_from} → {r.date_to}"
        elif r.date_from:
            date_range = f"{r.date_from} → present"
        elif r.date_to:
            date_range = f"up to {r.date_to}"

        yield Header()
        yield VerticalScroll(
            Static("Download filter summary", id="heading"),
            Static(f"Genres:    {', '.join(r.genres) or 'all'}"),
            Static(f"Subgenres: {', '.join(r.subgenres) or 'all'}"),
            Static(f"Artists:   {', '.join(r.artists) or 'all'}"),
            Static(f"Dates:     {date_range}"),
            Button("Confirm & queue", id="start", variant="success"),
            Button("Back", id="back"),
            Button("Cancel", id="cancel", variant="error"),
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            self.dismiss(("confirm", None))
        elif event.button.id == "back":
            self.dismiss(("back", None))
        else:
            self.dismiss(("cancel", None))

    def action_go_back(self) -> None:
        self.dismiss(("back", None))

    def action_bypass(self) -> None:
        self.dismiss(("bypass", None))


async def run_wizard_flow(
    app: App, genres: list[RankEntry], subgenres: list[RankEntry], artists: list[RankEntry]
) -> WizardResult:
    """Drives the genre/subgenre/artist/date/confirm screens on top of whatever
    App is currently running. Shared by the standalone WizardApp (tests, and
    plain wizard-only use) and MainApp (the full TUI session)."""
    r = WizardResult()
    step = "genres"
    while True:
        if step == "genres":
            if not genres:
                step = "subgenres"
                continue
            action, data = await app.push_screen_wait(SelectScreen("Genres", genres))
            if action == "bypass":
                r.bypass = True
                return r
            if action == "back":
                r.cancelled = True
                return r
            r.genres = data
            step = "subgenres"

        elif step == "subgenres":
            if not subgenres:
                step = "artists"
                continue
            action, data = await app.push_screen_wait(SelectScreen("Subgenres", subgenres))
            if action == "bypass":
                r.bypass = True
                return r
            if action == "back":
                step = "genres"
                continue
            r.subgenres = data
            step = "artists"

        elif step == "artists":
            if not artists:
                step = "date_from"
                continue
            action, data = await app.push_screen_wait(SelectScreen("Artists (by track count)", artists))
            if action == "bypass":
                r.bypass = True
                return r
            if action == "back":
                step = "subgenres" if subgenres else "genres"
                continue
            r.artists = data
            step = "date_from"

        elif step == "date_from":
            action, data = await app.push_screen_wait(
                DateScreen("Download from date (leave empty for all)", end_of_period=False)
            )
            if action == "bypass":
                r.bypass = True
                return r
            if action == "back":
                step = "artists" if artists else ("subgenres" if subgenres else "genres")
                continue
            r.date_from = data
            step = "date_to"

        elif step == "date_to":
            action, data = await app.push_screen_wait(
                DateScreen("Download up to date (leave empty for all)", end_of_period=True)
            )
            if action == "bypass":
                r.bypass = True
                return r
            if action == "back":
                step = "date_from"
                continue
            r.date_to = data
            step = "confirm"

        elif step == "confirm":
            action, _ = await app.push_screen_wait(ConfirmScreen(r))
            if action == "bypass":
                r.bypass = True
                return r
            if action == "cancel":
                r.cancelled = True
                return r
            if action == "back":
                step = "date_to"
                continue
            return r


class WizardApp(App):
    CSS = NAUTICAL_CSS

    def __init__(self, genres: list[RankEntry], subgenres: list[RankEntry], artists: list[RankEntry]):
        super().__init__()
        self.genres = genres
        self.subgenres = subgenres
        self.artists = artists
        self.result = WizardResult()

    def on_mount(self) -> None:
        self._run_wizard()

    @work
    async def _run_wizard(self) -> None:
        self.result = await run_wizard_flow(self, self.genres, self.subgenres, self.artists)
        self.exit(self.result)


def run_wizard(genres: list[RankEntry], subgenres: list[RankEntry], artists: list[RankEntry]) -> WizardResult:
    app = WizardApp(genres, subgenres, artists)
    result = app.run()
    return result if result is not None else WizardResult(cancelled=True)


QUALITY_OPTIONS = [("Lossless (FLAC)", "lossless"), ("High (256kbps AAC)", "high"), ("Medium (128kbps AAC)", "medium")]
TRACK_EXISTS_OPTIONS = [
    ("Skip", "skip"), ("Update tags", "update"), ("Overwrite", "overwrite"), ("Error", "error"),
]
KEY_SYSTEM_OPTIONS = [
    ("Standard (e.g. A Minor)", "standard"),
    ("Standard short (e.g. Am)", "standard-short"),
    ("OpenKey (e.g. 8m)", "openkey"),
    ("Camelot (e.g. 8A)", "camelot"),
]


@dataclass
class FieldSpec:
    key: str
    label: str
    kind: str  # "input" | "password" | "select" | "switch" | "int"
    options: list[tuple[str, str]] | None = None


ACCOUNT_FIELDS = [
    FieldSpec("username", "Beatport username", "input"),
    FieldSpec("password", "Beatport password", "password"),
]

DOWNLOAD_FIELDS = [
    FieldSpec("downloads_directory", "Downloads directory", "input"),
    FieldSpec("quality", "Quality", "select", QUALITY_OPTIONS),
    FieldSpec("track_exists", "If track file already exists", "select", TRACK_EXISTS_OPTIONS),
    FieldSpec("max_global_workers", "Max concurrent releases/items", "int"),
    FieldSpec("max_download_workers", "Max concurrent track downloads", "int"),
    FieldSpec("proxy", "HTTP(S) proxy URL (blank for none)", "input"),
]

NAMING_FIELDS = [
    FieldSpec("sort_by_context", "Sort into label/artist/release subfolders", "switch"),
    FieldSpec("sort_by_label", "Group releases under a label subfolder too", "switch"),
    FieldSpec("force_release_directories", "Force release subfolders in playlists/charts", "switch"),
    FieldSpec("track_number_padding", "Track number zero-padding (0 = auto)", "int"),
    FieldSpec("whitespace_character", "Replace spaces in filenames with", "input"),
    FieldSpec("artists_limit", "Max artists shown before using short form", "int"),
    FieldSpec("artists_short_form", "Short form label (e.g. VA)", "input"),
    FieldSpec("key_system", "Musical key notation", "select", KEY_SYSTEM_OPTIONS),
    FieldSpec("track_file_template", "Track filename template", "input"),
    FieldSpec("release_directory_template", "Release folder template", "input"),
    FieldSpec("label_directory_template", "Label folder template", "input"),
    FieldSpec("artist_directory_template", "Artist folder template", "input"),
    FieldSpec("playlist_directory_template", "Playlist folder template", "input"),
    FieldSpec("chart_directory_template", "Chart folder template", "input"),
]

TAGGING_FIELDS = [
    FieldSpec("fix_tags", "Write tags on download", "switch"),
    FieldSpec("cover_size", "Cover art size (WxH, e.g. 1400x1400)", "input"),
    FieldSpec("keep_cover", "Keep cover.jpg in release folders", "switch"),
]

SETTINGS_SECTIONS = {
    "account": ("Account", ACCOUNT_FIELDS),
    "downloads": ("Downloads & files", DOWNLOAD_FIELDS),
    "naming": ("Folder & file naming", NAMING_FIELDS),
    "tagging": ("Tagging & covers", TAGGING_FIELDS),
}


class FieldFormScreen(Screen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, cfg, fields: list[FieldSpec], config_path=None):
        super().__init__()
        self.title_text = title
        self.cfg = cfg
        self.fields = fields
        self.config_path = config_path

    def compose(self) -> ComposeResult:
        yield Header()
        widgets: list = [Static(self.title_text, id="heading")]
        for f in self.fields:
            widgets.append(Static(f.label))
            value = getattr(self.cfg, f.key)
            if f.kind == "select":
                widgets.append(Select(f.options, value=value, id=f.key, allow_blank=False))
            elif f.kind == "switch":
                widgets.append(Switch(value=bool(value), id=f.key))
            elif f.kind == "password":
                widgets.append(Input(value=str(value), id=f.key, password=True))
            else:
                widgets.append(Input(value=str(value), id=f.key))
        widgets.append(Static("", id="form_error"))
        widgets.append(Button("Save", id="save", variant="success"))
        widgets.append(Button("Cancel", id="cancel"))
        yield VerticalScroll(*widgets)
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "save":
            self.dismiss(False)
            return

        for f in self.fields:
            widget = self.query_one(f"#{f.key}")
            if f.kind == "int":
                try:
                    setattr(self.cfg, f.key, int(widget.value))
                except ValueError:
                    self.query_one("#form_error", Static).update(f"[red]{f.label} must be a whole number[/red]")
                    return
            else:
                setattr(self.cfg, f.key, widget.value)

        if self.config_path:
            config_module.save(self.cfg, self.config_path)
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class SettingsMenuScreen(Screen):
    """Lists config sections; Escape closes (unless `required`, which forces
    filling in Account + Downloads with a real username/password/folder first —
    used for first-run setup)."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, cfg, config_path, required: bool = False):
        super().__init__()
        self.cfg = cfg
        self.config_path = config_path
        self.required = required

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        yield Header()
        heading = "First-run setup — fill in Account and Downloads, then Close" if self.required else "Settings"
        yield Static(heading, id="heading")
        yield OptionList(
            *(Option(title, id=key) for key, (title, _fields) in SETTINGS_SECTIONS.items()),
            id="menu",
        )
        yield Static("", id="menu_error")
        yield Footer()

    def on_option_list_option_selected(self, event) -> None:
        self._open_section(event.option_id)

    @work
    async def _open_section(self, section_key: str) -> None:
        title, fields = SETTINGS_SECTIONS[section_key]
        await self.app.push_screen_wait(FieldFormScreen(title, self.cfg, fields, self.config_path))

    def action_close(self) -> None:
        if self.required:
            missing = []
            if not self.cfg.username:
                missing.append("username")
            if not self.cfg.password:
                missing.append("password")
            if not self.cfg.downloads_directory:
                missing.append("downloads directory")
            if missing:
                self.query_one("#menu_error", Static).update(
                    f"[red]Still needed before you can continue: {', '.join(missing)}[/red]"
                )
                return
        self.dismiss(True)


class SearchResultsScreen(Screen):
    BINDINGS = [
        Binding("enter", "confirm", "Add selected", priority=True),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, entries: list[tuple[str, str, str]]):
        super().__init__()
        self.entries = entries

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Search results — space/enter on a row to toggle, Enter to add selected", id="heading")
        options = [(f"[{kind}] {label}", i) for i, (kind, label, _value) in enumerate(self.entries)]
        yield SelectionList[int](*options, id="list")
        yield Footer()

    def action_confirm(self) -> None:
        self.dismiss(("confirm", list(self.query_one(SelectionList).selected)))

    def action_cancel(self) -> None:
        self.dismiss(("cancel", None))


class MainScreen(Screen):
    BINDINGS = [
        Binding("ctrl+d", "start_downloads", "Download queue"),
        Binding("ctrl+s", "open_settings", "Settings"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "① Paste a label/artist/track/release URL, or a search query, below.\n"
            "② Repeat for as many as you like — each one gets added to the queue.\n"
            "③ Press [b]ctrl+d[/b] to start downloading everything queued.",
            id="hint",
        )
        yield Static("Queue (0): empty", id="queue_display")
        yield Static("", id="status")
        yield Input(placeholder="Label/artist/track/release URL, or search query...", id="main_input")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        event.input.value = ""
        if value:
            self.app.handle_submission(value)

    def action_start_downloads(self) -> None:
        self.app.action_start_downloads()

    def action_open_settings(self) -> None:
        self.app.open_settings()

    def update_queue_display(self, queue: list[str]) -> None:
        text = f"Queue ({len(queue)}): " + (", ".join(queue) if queue else "empty")
        self.query_one("#queue_display", Static).update(text)

    def set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)


class MainApp(App):
    CSS = NAUTICAL_CSS
    BINDINGS = [
        Binding("ctrl+d", "start_downloads", "Download queue", priority=True),
        Binding("ctrl+s", "settings", "Settings", priority=True),
    ]

    def __init__(self, cfg, config_path, make_clients, first_run: bool = False):
        super().__init__()
        self.cfg = cfg
        self.config_path = config_path
        self.make_clients = make_clients
        self.first_run = first_run
        self.bp = None
        self.bs = None
        self.queue: list[str] = []
        self.main_screen = MainScreen()

    def on_mount(self) -> None:
        self.push_screen(self.main_screen)
        self._startup()

    @work
    async def _startup(self) -> None:
        if self.first_run:
            self.main_screen.set_status("Welcome — set up your account and downloads folder to continue.")
            await self.push_screen_wait(SettingsMenuScreen(self.cfg, self.config_path, required=True))
            config_module.save(self.cfg, self.config_path)
        await self._login()

    async def _login(self) -> None:
        self.main_screen.set_status("Logging in...")
        try:
            self.bp, self.bs = await self._run_in_thread(self.make_clients, self.cfg)
            self.main_screen.set_status("Ready.")
        except Exception as e:
            self.main_screen.set_status(f"[red]Login failed: {e}[/red] — check Account settings (ctrl+s) and try again.")

    def action_start_downloads(self) -> None:
        self.exit(self.queue)

    def open_settings(self) -> None:
        self._open_settings()

    @work
    async def _open_settings(self) -> None:
        old_username, old_password = self.cfg.username, self.cfg.password
        saved = await self.push_screen_wait(SettingsMenuScreen(self.cfg, self.config_path))
        if not saved:
            return
        if self.bp is None or (self.cfg.username, self.cfg.password) != (old_username, old_password):
            await self._login()
        else:
            self.main_screen.set_status("Settings saved.")

    def handle_submission(self, raw: str) -> None:
        self._process_input(raw)

    async def _run_in_thread(self, fn, *args):
        worker = self.run_worker(lambda: fn(*args), thread=True)
        return await worker.wait()

    @work
    async def _process_input(self, raw: str) -> None:
        if self.bp is None or self.bs is None:
            self.main_screen.set_status("Still logging in — one moment...")
            return
        self.main_screen.set_status("Working...")
        try:
            if raw.startswith("https://www.beatport.com") or raw.startswith("https://www.beatsource.com"):
                try:
                    link = parse_url(raw)
                except Exception as e:
                    self.main_screen.set_status(f"Invalid URL: {e}")
                    return
                if link.type in (LABEL_LINK, ARTIST_LINK):
                    await self._run_label_wizard(raw, link)
                else:
                    self.queue.append(raw)
                    self.main_screen.set_status(f"Queued: {raw} — paste another URL, or press ctrl+d to start downloading.")
            else:
                await self._run_search(raw)
        finally:
            self.main_screen.update_queue_display(self.queue)

    async def _run_label_wizard(self, raw_url: str, link) -> None:
        client = self.bs if link.store == "beatsource" else self.bp
        self.main_screen.set_status("Scanning — this may take a while for large catalogues...")

        def progress(msg: str) -> None:
            self.call_from_thread(self.main_screen.set_status, msg)

        try:
            if link.type == LABEL_LINK:
                stats = await self._run_in_thread(scan_label, client, link, progress)
            else:
                stats = await self._run_in_thread(scan_artist, client, link, progress)
        except Exception as e:
            self.main_screen.set_status(f"Scan failed: {e}")
            return

        genres = rank_map(stats.genres)
        subgenres = rank_map(stats.subgenres)
        artists = rank_map(stats.artists)
        result = await run_wizard_flow(self, genres, subgenres, artists)

        if result.cancelled:
            self.main_screen.set_status("Cancelled.")
            return
        if result.bypass:
            self.cfg.filter_genres = []
            self.cfg.filter_subgenres = []
            self.cfg.filter_artists = []
            self.cfg.filter_publish_date_from = ""
            self.cfg.filter_publish_date_to = ""
        else:
            self.cfg.filter_genres = result.genres
            self.cfg.filter_subgenres = result.subgenres
            self.cfg.filter_artists = result.artists
            self.cfg.filter_publish_date_from = result.date_from
            self.cfg.filter_publish_date_to = result.date_to
        self.queue.append(raw_url)
        self.main_screen.set_status(f"Queued: {raw_url} — paste another URL, or press ctrl+d to start downloading.")

    async def _run_search(self, query: str) -> None:
        store_tag, trimmed = extract_store_tag(query)
        client = self.bs if store_tag == "beatsource" else self.bp

        try:
            label_results = await self._run_in_thread(client.search_labels, trimmed)
        except Exception as e:
            self.main_screen.set_status(f"Search failed: {e}")
            return
        try:
            results = await self._run_in_thread(client.search, trimmed)
        except Exception:
            results = {"tracks": [], "releases": []}

        entries: list[tuple[str, str, str]] = []
        for lbl in label_results.results:
            entries.append(("label", lbl.name, lbl.store_url()))
        for t in results["tracks"]:
            artists_str = ", ".join(a["name"] for a in t.artists[: self.cfg.artists_limit])
            entries.append(("track", f"{artists_str} - {t.name} ({t.mix_name})", t.url))
        for r in results["releases"]:
            artists_str = ", ".join(a["name"] for a in r.artists[: self.cfg.artists_limit])
            entries.append(("release", f"{artists_str} - {r.name} [{r.label.name}]", r.url))

        if not entries:
            self.main_screen.set_status("No results found.")
            return

        action, indices = await self.push_screen_wait(SearchResultsScreen(entries))
        if action != "confirm" or not indices:
            self.main_screen.set_status("Cancelled.")
            return

        added = 0
        for i in indices:
            kind, _label, value = entries[i]
            if kind == "label":
                await self._run_label_wizard(value, parse_url(value))
            else:
                self.queue.append(value)
            added += 1
        self.main_screen.set_status(f"Added {added} item(s) to queue.")


def run_main_app(cfg, config_path, make_clients, first_run: bool = False) -> list[str]:
    app = MainApp(cfg, config_path, make_clients, first_run=first_run)
    result = app.run()
    return result if result is not None else []
