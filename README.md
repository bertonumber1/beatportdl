# beatportdl-webui

A from-scratch Python rewrite of BeatportDL — no Go, no CGO, no compiled TagLib/ffmpeg
toolchain. Same core capability (Beatport/Beatsource FLAC downloader with label/artist
filtering), delivered two ways:

- **A full-screen terminal UI** ([Textual](https://github.com/Textualize/textual)) — dark,
  professional theme, live scan progress, settings menu built in.
- **A web UI** (FastAPI + server-sent events) — the same capability from any browser on your
  network: live per-track progress bars, real album art, a genre/subgenre/artist filter wizard,
  full settings control, and an installable "app icon" via its web manifest.

Runs on Linux (amd64/arm64), Windows, and macOS. Docker images are multi-arch
(`linux/amd64` + `linux/arm64`).

## Screenshots (web UI)

| Queue | Filter wizard |
|---|---|
| ![Queue](screenshots/queue.png) | ![Filter wizard](screenshots/filter-wizard.png) |

| Scanning a label | Live downloads |
|---|---|
| ![Scanning](screenshots/scanning.png) | ![Now downloading](screenshots/now-downloading.png) |

## What changed vs. the original Go version

- **Two interfaces, one engine** — `bpdl` (TUI/CLI) and `bpdl-web` (browser dashboard) share
  the exact same auth/catalog/download/tagging code (`bpdl/handlers.py`, `bpdl/download.py`,
  `bpdl/tagging.py`). Nothing is duplicated between them.
- **Live progress everywhere** — scanning a label/artist streams status in real time; the web
  UI additionally shows real byte-level progress bars per track as they download.
- **`q` bypasses filtering at any step** (TUI) / **"Queue everything, no filter"** (web) — skip
  genre/subgenre/artist/date filtering entirely and queue the whole label/artist catalogue
  unfiltered.
- **Full settings control from both UIs**, not just a config file — account, downloads,
  folder/file naming templates, and tagging, all writing straight back to the YAML config.
  First run walks you through the required fields before unlocking anything else.
- **Album/track art recheck** — a library-maintenance tool (web UI → Settings) that walks your
  downloads folder, finds tracks with missing or broken embedded artwork, and re-fetches +
  re-embeds it from Beatport/Beatsource using the release ID now embedded in every download's
  tags (`BEATPORT_RELEASE_ID` / `BEATPORT_TRACK_ID`).
- **No C++ build chain.** Tagging is `mutagen` (FLAC Vorbis comments + real MP4 atoms), not a
  vendored TagLib. No ffmpeg dependency — the AAC-via-HLS quality path from the original tool
  was dropped since this setup is FLAC-first (AAC 128/256kbps still available as quality
  options, served directly by Beatport's API with no local transcoding).
- **Bulletproof-er downloads** — atomic writes (`.part` file + rename), retry with backoff on
  flaky network calls, and a run summary (downloaded/skipped/failed) at the end.
- **Same skip logic** as the original: pre-release, territory-restricted, and generically
  unavailable (403/404) tracks are silently skipped and logged, both during download and during
  label scanning (a territory-restricted release doesn't abort the whole scan).
- **Windows build restored** — a standalone `bpdl.exe` / `bpdl-web.exe`, built via PyInstaller
  in CI on every release, no Python install required.

## Quick start — Docker (recommended)

Pull the multi-arch image (works on amd64 and arm64 hosts — Raspberry Pi, Apple Silicon, etc.
— without any extra flags):

```bash
docker pull ghcr.io/bertonumber1/beatportdl-webui:latest
```

Or build locally:

```bash
docker compose build
docker compose up -d bpdl-web   # web UI, persistent, http://<host>:8095
docker compose run --rm bpdl    # TUI, interactive
```

`compose.yml` routes both through a `gluetun` VPN container (`network_mode: container:gluetun`)
matching the original Go build's networking — drop that line if you don't use a VPN container.
Config lives in `./config/bpdl-config.yml` (created on first run/first save); downloads land
wherever the `/downloads` volume mount points — edit that in `compose.yml` to match your setup.

### Building/publishing a multi-arch image yourself

```bash
docker buildx create --name multiarch --driver docker-container --use
docker buildx build --platform linux/amd64,linux/arm64 -t you/beatportdl-webui:latest --push .
```

## Quick start — native Linux / macOS (no Docker)

Works on amd64 and arm64 (Apple Silicon) — every dependency ships proper wheels for both.

```bash
pip install .
bpdl        # TUI
bpdl-web    # web UI on :8095
```

## Quick start — Windows

**Option A — standalone .exe, no Python required:** download
`beatportdl-webui-windows-x64.zip` from the
[Releases page](https://github.com/bertonumber1/beatportdl/releases), unzip, run `bpdl.exe`
(TUI) or `bpdl-web.exe` (web UI, then open `http://localhost:8095`).

**Option B — pip, if you already have Python 3.10+:**

```powershell
pip install .
bpdl
bpdl-web
```

### Building the Windows .exe yourself

```powershell
pip install . pyinstaller
pyinstaller --onefile --name bpdl --collect-all textual --collect-all rich scripts\win_bpdl.py
pyinstaller --onefile --name bpdl-web --collect-all fastapi --collect-all starlette --collect-all uvicorn --add-data "bpdl/webui/static;bpdl/webui/static" scripts\win_bpdl_web.py
```

Both `.exe`s land in `dist\`. This is exactly what `.github/workflows/release.yml` runs on
every tagged release.

## Using the web UI

1. Open `http://<host>:8095`. First run prompts for username/password/downloads directory —
   nothing else is reachable until those are set.
2. Paste a **label/artist URL** → live scan progress → a chip-based genre/subgenre/artist/date
   filter picker (bar length = relative track count) → "Queue with filters" or "Queue
   everything (no filter)".
3. Paste a **track/release/playlist/chart URL** → added straight to the queue with its cover
   art.
4. Type a **search query** (optionally `@beatsource daft punk`) → pick results from a grid,
   add selected.
5. **Start downloading** → live cards per track with real progress bars, a running
   downloaded/skipped/failed stats bar, toasts on completion.
6. Gear icon → **Settings** any time (all fields, plus the album/track art recheck tool under
   "Library maintenance").

## Using the TUI

- Same wizard flow as the web UI; press `q` at any wizard step to bypass filtering entirely.
- `ctrl+s` → Settings menu (Account, Downloads & files, Folder & file naming, Tagging &
  covers).
- `ctrl+d` → start downloading everything queued so far.

### CLI batch mode

```bash
bpdl https://www.beatport.com/track/example/12345 another-url.txt
bpdl --scan https://www.beatport.com/label/example/6446
```

Passing URLs (or `.txt` files of URLs) as arguments skips the TUI entirely and downloads
immediately — no filtering wizard applies, same as pasting a track/release URL directly.
`--scan` lists genre/subgenre/BPM-range/top-artist stats for a label or artist without
downloading anything.

## Config reference

Everything under `config/bpdl-config.yml` is editable via either UI's Settings screen, with one
exception: `tag_mappings` (which controls exactly which Vorbis/MP4 tag each metadata field maps
to) isn't exposed there — hand-edit the YAML if you need to change it from the built-in
defaults.

All three quality tiers from the original tool are supported — `lossless` (FLAC), `high` (AAC
256kbps), `medium` (AAC 128kbps) — served directly by Beatport/Beatsource's API, no ffmpeg or
local transcoding involved.
