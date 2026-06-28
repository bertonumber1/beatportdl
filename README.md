```
                                __  _____
  __  ______  _________  ____  / /_|__  /____
 / / / / __ \/ ___/ __ \/ __ \/ //_//_ </ __ \
/ /_/ / / / (__  ) /_/ / /_/ / ,< ___/ / / / /
\__,_/_/ /_/____/ .___/\____/_/|_/____/_/ /_/
               /_/
```

# BeatportDL

Beatport & Beatsource downloader (FLAC, AAC)

*Requires an active [Beatport](https://stream.beatport.com/) or [Beatsource](https://stream.beatsource.com/) streaming plan.*

![Screenshot](/screenshots/main.png?raw=true "Screenshot")

> **Credit** — This tool was created by [**unspok3n**](https://github.com/unspok3n). All core functionality, API integration, tag writing, and file handling is their work. This fork adds filtering and an interactive wizard on top of that foundation. Please star the [original repository](https://github.com/unspok3n/beatportdl) if you find the tool useful.

---

> ### PLEASE READ BEFORE RAISING ISSUES
>
> **This is a personal fork.** If you encounter a problem, bug, or unexpected behaviour, please **do not contact or open issues against [unspok3n's original repository](https://github.com/unspok3n/beatportdl)** — it is not their problem and they should not be bothered with issues that originate here.
>
> For anything specific to this fork, raise it here or contact **[bertonumber1](https://github.com/bertonumber1)** directly.
>
> All credit, praise, and recognition goes entirely to [**unspok3n**](https://github.com/unspok3n) for the extraordinary work in building this project from the ground up. The wizard and filtering additions in this fork are built on top of their foundation and exist purely for fun and educational purposes. Please respect the original author.

---

Added in this fork
---

This fork adds **label/artist filtering** and an **interactive download wizard** on top of the original BeatportDL.

### Interactive Wizard

When you paste a **label or artist URL** at the prompt, the wizard automatically kicks in:

1. Scans the full catalogue (shows live progress)
2. Presents a numbered genre menu with track counts — pick by number, `*` for all, or Enter to skip
3. Same for subgenres
4. Optional date-from filter (`1996`, `1996-06`, or `1996-06-01`)
5. Shows a summary and asks to confirm before downloading

```
Enter label/artist URL, search query, or label name: crestwave

[ Labels ]
   1. Crestwave Records

Enter result number(s): 1

Scanning Crestwave Records — please wait...
  Scanning release 47 — 312 tracks found so far...

Genres found:
   1. House                                      198 tracks
   2. Hard Techno                                 91 tracks
   3. Dance                                       23 tracks
Select (e.g. 1,3  |  * for all  |  Enter to skip filter): 1,3

From date (e.g. 1996 or 1996-06-01, Enter for all): 1996

--- Download filter summary ---
  Genres:     House, Dance
  Subgenres:  all
  From date:  1996-01-01

Start download? (y/n): y
```

You can also **search by label name** — type the label name at the prompt and matching labels appear alongside track/release results.

### Scan Mode

Use `--scan` to inspect a label or artist without downloading anything:

```shell
./beatportdl --scan https://www.beatport.com/label/label-name/12345
```

Output shows all genres, subgenres, BPM range, and top artists with track counts — useful for knowing exactly what filter values to use.

### Config-based Filtering

Filters can also be set permanently in `beatportdl-config.yml` for non-interactive / scripted use (e.g. passing a URL as a CLI argument). All filters are **AND between types, OR within a list** — so genres AND subgenres AND date must all match.

New config options:

| Option | Type | Description |
|---|---|---|
| `filter_genres` | String list | Only download tracks whose genre matches one of these (case-insensitive) |
| `filter_subgenres` | String list | Only download tracks whose subgenre matches one of these (case-insensitive) |
| `filter_artists` | String list | Only download tracks featuring at least one of these artists (case-insensitive) |
| `filter_publish_date_from` | String `YYYY-MM-DD` | Only download tracks published on or after this date |
| `filter_publish_date_to`   | String `YYYY-MM-DD` | Only download tracks published on or before this date |

Example — only House and Dance tracks from 1996 onwards:

```yaml
filter_genres:
  - House
  - Dance

filter_publish_date_from: "1996-01-01"
filter_publish_date_to:   "2024-12-31"
```

Filtered-out tracks log `skipped (filter)` so you can verify what is being excluded.

### Docker build

A multi-stage `Dockerfile` is included that builds from source (including TagLib 2.x) — no pre-built binary required. See the `Dockerfile` in the root of the repo.

---

Installation Guide
---

### Requirements

- An active [Beatport](https://stream.beatport.com/) or [Beatsource](https://stream.beatsource.com/) **Professional** streaming subscription (for FLAC). Lower tiers support AAC quality.
- Your Beatport username and password.

---

### Linux / Ubuntu

There are two ways to run on Linux — via Docker (recommended, no build tools needed) or as a native binary.

#### Option A — Docker (recommended)

Docker handles all dependencies automatically. No Go compiler or TagLib installation required.

**1. Install Docker**

If you don't have Docker installed:
```bash
sudo apt update
sudo apt install docker.io docker-compose-plugin
sudo usermod -aG docker $USER
```
Log out and back in for the group change to take effect.

**2. Clone this repo**
```bash
git clone https://github.com/bertonumber1/beatportdl
cd beatportdl
```

**3. Build the image**
```bash
docker compose build
```
This compiles everything from source including TagLib 2.x — takes a few minutes the first time, cached after that.

**4. Create your config**

Edit `config/beatportdl-config.yml`:
```yaml
username: your_beatport_username
password: your_beatport_password

quality: lossless
downloads_directory: /downloads

sort_by_context: true
sort_by_label: true

track_exists: skip

release_directory_template: "{catalog_number} - {artists} - {name} ({year})"

# --- Optional filters ---
# filter_genres:
#   - House
#   - Dance
#
# filter_subgenres:
#   - Pont Aeri
#
# filter_publish_date_from: "1996-01-01"
# filter_publish_date_to:   "2024-12-31"
```

**5. Create a launch script**

Create `/usr/local/bin/beatportdl` (or anywhere on your PATH):
```bash
sudo nano /usr/local/bin/beatportdl
```
Paste:
```bash
#!/bin/bash
docker run --rm -it \
  --user $(id -u):$(id -g) \
  -v /path/to/beatportdl/config:/config \
  -v /path/to/your/music:/downloads \
  beatportdl-beatportdl:latest "$@"
```
Replace the paths to match your setup, then:
```bash
sudo chmod +x /usr/local/bin/beatportdl
```

**6. Run it**
```bash
beatportdl
```

---

#### Option B — Native binary

Download the latest `beatportdl-linux-amd64` from the [Releases](https://github.com/bertonumber1/beatportdl/releases) page.

```bash
chmod +x beatportdl-linux-amd64
./beatportdl-linux-amd64
```

On first run it will ask for your username, password, and downloads directory, then create a `beatportdl-config.yml` you can edit.

---

### Windows (no Docker required)

> All the filtering, wizard, and scan features are included in the Windows build. No installation needed — just a folder with two files.

**1. Download the binary**

Go to the [Releases](https://github.com/bertonumber1/beatportdl/releases) page of this fork and download `beatportdl-windows-amd64.exe`.

**2. Set up your folder**

Create a folder anywhere you like, for example `C:\BeatportDL\`. Place the `.exe` inside it.

**3. Create your config file**

In the same folder, create a new text file called `beatportdl-config.yml` (make sure Windows doesn't add `.txt` to the end — rename it if needed).

Paste the following into it, filling in your own details:

```yaml
username: your_beatport_username
password: your_beatport_password

quality: lossless
downloads_directory: C:\Users\YourName\Music\Beatport

sort_by_context: true
sort_by_label: true

track_exists: skip

release_directory_template: "{catalog_number} - {artists} - {name} ({year})"

# --- Optional filters ---
# Remove the # from any line you want to activate.
# Genre and subgenre names must match exactly what --scan shows (case-insensitive).
#
# filter_genres:
#   - House
#   - Dance
#
# filter_subgenres:
#   - Pont Aeri
#
# filter_publish_date_from: "1996-01-01"
# filter_publish_date_to:   "2024-12-31"
```

> **Windows path tip:** use either forward slashes (`C:/Users/YourName/Music`) or double backslashes (`C:\\Users\\YourName\\Music`) — both work.

**4. Run it**

Open **PowerShell** (right-click the Start menu → Windows PowerShell), navigate to your folder, and run:

```powershell
cd C:\BeatportDL
.\beatportdl-windows-amd64.exe
```

Or simply double-click the `.exe` from Explorer — a terminal window will open automatically.

You will see:
```
Enter label/artist URL, search query, or label name:
```

**5. Downloading from a label — using the wizard**

Paste a Beatport label URL (copy it from your browser while browsing a label page) or just type the label name:

```
Enter label/artist URL, search query, or label name: crestwave
```

The wizard will:
1. Show matching labels — type the number and press Enter
2. Scan the full catalogue (shows progress as it goes)
3. Display all genres found with track counts — pick by number (e.g. `1,3`), type `*` for all, or press Enter to skip the filter
4. Same for subgenres
5. Ask for a date — type a year like `1996` or a full date `1996-06-01`, or press Enter for all time
6. Show a summary and ask `y/n` before downloading

**6. Scanning a label without downloading**

To see what genres, subgenres, BPM ranges, and artists are on a label before committing to a download:

```powershell
.\beatportdl-windows-amd64.exe --scan https://www.beatport.com/label/label-name/12345
```

Use the genre/subgenre names it shows as your filter values in the config or in the wizard.

**7. Useful tips**

- `beatportdl-credentials.json` appears after first login — keep it, it saves you logging in every time
- Add `-q` to quit automatically after finishing instead of looping back to the prompt:
  ```powershell
  .\beatportdl-windows-amd64.exe -q https://www.beatport.com/label/label-name/12345
  ```
- To filter permanently (useful if you always want the same genres), uncomment and edit the `filter_genres` / `filter_subgenres` / `filter_publish_date_from` / `filter_publish_date_to` lines in the config file — filters set there apply to every download, bypassing the wizard
- Skipped tracks are logged as `skipped (filter)` so you can verify the filter is working

---

Setup
---
1. [Download](https://github.com/unspok3n/beatportdl/releases/) or [build](#building) BeatportDL.

     *Compiled binaries for Windows, macOS (amd64, arm64) and Linux (amd64, arm64) are available on the [Releases](https://github.com/unspok3n/beatportdl/releases) page.* \
     *Don't forget to set the execute permission on unix systems, e.g., chmod +x beatportdl-darwin-arm64*

2. Run beatportdl (e.g. `./beatportdl-darwin-arm64`), then specify the:
   - Beatport username
   - Beatport password
   - Downloads directory
   - Audio quality

3. OPTIONAL: Customize a config file. Create a new config file by running:
```shell
./beatportdl
```
This will create a new `beatportdl-config.yml` file. You can put the following options and values into the config file:

---
| Option                        | Default Value                             | Type       | Description                                                                                                                                                                               |
|-------------------------------|-------------------------------------------|------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `username`                    |                                           | String     | Beatport username                                                                                                                                                                         |
| `password`                    |                                           | String     | Beatport password                                                                                                                                                                         |
| `quality`                     | lossless                                  | String     | Download quality *(medium-hls, medium, high, lossless)*                                                                                                                                   |
| `show_progress`               | true                                      | Boolean    | Enable progress bars                                                                                                                                                                      |
| `write_error_log`             | false                                     | Boolean    | Write errors to `error.log`                                                                                                                                                               |
| `max_download_workers`        | 15                                        | Integer    | Concurrent download jobs limit                                                                                                                                                            |
| `max_global_workers`          | 15                                        | Integer    | Concurrent global jobs limit                                                                                                                                                              |
| `downloads_directory`         |                                           | String     | Location for the downloads directory                                                                                                                                                      |
| `sort_by_context`             | false                                     | Boolean    | Create a directory for each release, playlist, chart, label, or artist                                                                                                                    |
| `sort_by_label`               | false                                     | Boolean    | Use label names as parent directories for releases (requires `sort_by_context`)                                                                                                           |
| `force_release_directories`   | false                                     | Boolean    | Create release directories inside chart and playlist folders (requires `sort_by_context`)                                                                                                 |
| `track_exists`                | update                                    | String     | Behavior when track file already exists                                                                                                                                                   |
| `track_number_padding`        | 2                                         | Integer    | Track number padding for filenames and tag mappings (when using `track_number_with_padding` or `release_track_count_with_padding`)<br/> Set to 0 for dynamic padding based on track count |
| `cover_size`                  | 1400x1400                                 | String     | Cover art size for `keep_cover` and track metadata (if `fix_tags` is enabled)  *[max: 1400x1400]*                                                                                         |
| `keep_cover`                  | false                                     | Boolean    | Download cover art file (cover.jpg) to the context directory (requires `sort_by_context`)                                                                                                 |
| `fix_tags`                    | true                                      | Boolean    | Enable tag writing capabilities                                                                                                                                                           |
| `tag_mappings`                | *Listed below*                            | String Map | Custom tag mappings                                                                                                                                                                       |
| `track_file_template`         | {number}. {artists} - {name} ({mix_name}) | String     | Track filename template                                                                                                                                                                   |
| `release_directory_template`  | [{catalog_number}] {artists} - {name}     | String     | Release directory template                                                                                                                                                                |
| `playlist_directory_template` | {name} [{created_date}]                   | String     | Playlist directory template                                                                                                                                                               |
| `chart_directory_template`    | {name} [{published_date}]                 | String     | Chart directory template                                                                                                                                                                  |
| `label_directory_template`    | {name} [{updated_date}]                   | String     | Label directory template                                                                                                                                                                  |
| `artist_directory_template`   | {name}                                    | String     | Artist directory template                                                                                                                                                                 |
| `whitespace_character`        |                                           | String     | Whitespace character for track filenames and release directories                                                                                                                          |
| `artists_limit`               | 3                                         | Integer    | Maximum number of artists allowed before replacing with `artists_short_form` (affects directories, filenames, and search results)                                                         |
| `artists_short_form`          | VA                                        | String     | Custom string to represent "Various Artists"                                                                                                                                              |
| `key_system`                  | standard-short                            | String     | Music key system used in filenames and tags                                                                                                                                               |
| `proxy`                       |                                           | String     | Proxy URL                                                                                                                                                                                 |

If the Beatport credentials are correct, you should also see the file `beatportdl-credentials.json` appear in the BeatportDL directory.
*If you accidentally entered an incorrect password and got an error, you can always manually edit the config file*

Download quality options, per Beatport/Beatsource subscription type:

| Option       | Description                                                                                                  | Requires at least              | Notes                                                                   |
|--------------|--------------------------------------------------------------------------------------------------------------|--------------------------------|-------------------------------------------------------------------------|
| `medium-hls` | 128 kbps AAC through `/stream` endpoint (IMPORTANT: requires [ffmpeg](https://www.ffmpeg.org/download.html)) | Essential / Beatsource         | Same as `medium` on Advanced but uses a slightly slower download method |
| `medium`     | 128 kbps AAC                                                                                                 | Advanced / Beatsource Pro+     |                                                                         |
| `high`       | 256 kbps AAC                                                                                                 | Professional / Beatsource Pro+ |                                                                         |
| `lossless`   | 44.1 kHz FLAC                                                                                                | Professional / Beatsource Pro+ |                                                                         |

Available `track_exists` options:
* `error` Log error and skip
* `skip` Skip silently
* `overwrite` Re-download
* `update` Update tags

Available template keywords for filenames and directories (`*_template`):
* Track: `id`,`name`,`mix_name`,`slug`,`artists`,`remixers`,`number`,`length`,`key`,`bpm`,`genre`,`subgenre`,`genre_with_subgenre`,`subgenre_or_genre`,`isrc`,`label`
* Release: `id`,`name`,`slug`,`artists`,`remixers`,`date`,`year`,`track_count`,`bpm_range`,`catalog_number`,`upc`,`label`
* Playlist: `id`,`name`,`first_genre`,`track_count`,`bpm_range`,`length`,`created_date`,`updated_date`
* Chart: `id`,`name`,`slug`,`first_genre`,`track_count`,`creator`,`created_date`,`published_date`,`updated_date`
* Artist: `id`, `name`, `slug`
* Label: `id`, `name`, `slug`, `created_date`, `updated_date`

Default `tag_mappings` config:
```yaml
tag_mappings:
   flac:
      track_name: "TITLE"
      track_artists: "ARTIST"
      track_number: "TRACKNUMBER"
      track_subgenre_or_genre: "GENRE"
      track_key: "KEY"
      track_bpm: "BPM"
      track_isrc: "ISRC"
   
      release_name: "ALBUM"
      release_artists: "ALBUMARTIST"
      release_date: "DATE"
      release_track_count: "TOTALTRACKS"
      release_catalog_number: "CATALOGNUMBER"
      release_label: "LABEL"
   m4a:
      track_name: "TITLE"
      track_artists: "ARTIST"
      track_number: "TRACKNUMBER"
      track_genre: "GENRE"
      track_key: "KEY"
      track_bpm: "BPM"
      track_isrc: "ISRC"
   
      release_name: "ALBUM"
      release_artists: "ALBUMARTIST"
      release_date: "DATE"
      release_track_count: "TOTALTRACKS"
      release_catalog_number: "CATALOGNUMBER"
      release_label: "LABEL"
```

As you can see, each key here represents a predefined value from either a release or a track that you can use to customize what is written to which tags. When you add an entry in the mappings for any format (for e.g., `flac`), only the tags that you specify will be written.

All tags by default are converted to uppercase, but since some M4A players might not recognize it, you can write the tag in lowercase and add the `_raw` suffix to bypass the conversion. *(This applies to M4A tags only)*

For e.g., Traktor doesn't recognize the track key tag in uppercase, so you have to add:
```yaml
tag_mappings:
   m4a:
      track_key: "initialkey_raw"
```

Available `tag_mappings` keys: `track_id`,`track_url`,`track_name`,`track_artists`,`track_artists_limited`,`track_remixers`,`track_remixers_limited`,`track_number`,`track_number_with_padding`,`track_number_with_total`,`track_genre`,`track_subgenre`,`track_genre_with_subgenre`,`track_subgenre_or_genre`,`track_key`,`track_bpm`,`track_isrc`,`release_id`,`release_url`,`release_name`,`release_artists`,`release_artists_limited`,`release_remixers`,`release_remixers_limited`,`release_date`,`release_year`,`release_track_count`,`release_track_count_with_padding`,`release_catalog_number`,`release_upc`,`release_label`,`release_label_url`

Available `key_system` options:

| System           | Example           |
|------------------|-------------------|
| `standard`       | Eb Minor, F Major |
| `standard-short` | Ebm, F            |
| `openkey`        | 7m, 12d           |
| `camelot`        | 2A, 7B            |

Proxy URL format example: `http://username:password@127.0.0.1:8080`

Usage
---

Run BeatportDL and enter Beatport or Beatsource URL or search query:
```shell
./beatportdl
Enter url or search query:
```
By default, search returns the results from beatport, if you want to search on beatsource instead, include `@beatsource` tag in the query

...or specify the URL using positional arguments:
```shell
./beatportdl https://www.beatport.com/track/strobe/1696999 https://www.beatport.com/track/move-for-me/591753
```
...or provide a text file with urls (separated by a newline)
```shell
./beatportdl file.txt file2.txt
```

URL types that are currently supported: **Tracks, Releases, Playlists, Charts, Labels, Artists**

Building
---
Required dependencies:
* [TagLib](https://github.com/taglib/taglib) >= 2.0
* [zlib](https://github.com/madler/zlib) >= 1.2.3
* [Zig C/C++ Toolchain](https://github.com/ziglang/zig) >= 0.14.0

BeatportDL uses [TagLib](https://taglib.org/) C bindings to handle audio metadata and therefore requires [CGO](https://go.dev/wiki/cgo)

Makefile is adapted for cross-compilation and uses [Zig toolchain](https://github.com/ziglang/zig)

To compile BeatportDL with Zig using Makefile, you must specify the paths to the C/C++ libraries folder and headers folder for the desired OS and architecture with `-L` (for libraries) and `-I` (for headers) flags using environment variables: `MACOS_ARM64_LIB_PATH`, `MACOS_AMD64_LIB_PATH`, `LINUX_AMD64_LIB_PATH`, `LINUX_ARM64_LIB_PATH`, `WINDOWS_AMD64_LIB_PATH`

One line example *(for unix and unix-like os)*
```shell
MACOS_ARM64_LIB_PATH="-L/usr/local/lib -I/usr/local/include" \
make darwin-arm64
```

You can also create an `.env` file in the project folder and specify all environment variables in it:
```
MACOS_ARM64_LIB_PATH=-L/libraries/for/macos-arm64 -I/headers/for/macos-arm64
MACOS_AMD64_LIB_PATH=-L/libraries/for/macos-amd64 -I/headers/for/macos-amd64
LINUX_AMD64_LIB_PATH=-L/libraries/for/linux-amd64 -I/headers/for/linux-amd64
LINUX_ARM64_LIB_PATH=-L/libraries/for/linux-arm64 -I/headers/for/linux-arm64
WINDOWS_AMD64_LIB_PATH=-L/libraries/for/windows-amd64 -I/headers/for/windows-amd64
```
