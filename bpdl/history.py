from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from bpdl import paths
from bpdl.tagging import read_embedded_ids

AUDIO_EXTENSIONS = (".flac", ".m4a")

STATUS_DOWNLOADED = "downloaded"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

_lock = threading.Lock()
_db_path: Path | None = None


def _path() -> Path:
    global _db_path
    if _db_path is None:
        _db_path, _ = paths.find_history_file()
    return _db_path


def _connect() -> sqlite3.Connection:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER,
                release_id INTEGER,
                store TEXT,
                track_name TEXT,
                artists TEXT,
                release_name TEXT,
                label TEXT,
                genre TEXT,
                subgenre TEXT,
                bpm INTEGER,
                key TEXT,
                quality TEXT,
                file_path TEXT,
                file_size_bytes INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                reason TEXT,
                downloaded_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_track_id ON downloads(track_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_release_id ON downloads(release_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_downloaded_at ON downloads(downloaded_at)")

        # CREATE TABLE IF NOT EXISTS doesn't add columns to a table that already
        # existed before this field was introduced — migrate explicitly.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(downloads)")}
        if "file_size_bytes" not in existing_cols:
            conn.execute("ALTER TABLE downloads ADD COLUMN file_size_bytes INTEGER DEFAULT 0")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_releases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                release_id INTEGER NOT NULL,
                label_url TEXT NOT NULL,
                release_name TEXT,
                expected_date TEXT,
                first_seen_at TEXT NOT NULL,
                UNIQUE(release_id, label_url)
            )
        """)


def record(
    track_id: int,
    release_id: int,
    store: str,
    track_name: str,
    artists: str,
    release_name: str,
    label: str,
    genre: str,
    subgenre: str,
    bpm: int,
    key: str,
    quality: str,
    file_path: str,
    status: str,
    reason: str = "",
    file_size_bytes: int = 0,
) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO downloads
               (track_id, release_id, store, track_name, artists, release_name, label,
                genre, subgenre, bpm, key, quality, file_path, file_size_bytes, status,
                reason, downloaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                track_id, release_id, store, track_name, artists, release_name, label,
                genre, subgenre, bpm, key, quality, file_path, file_size_bytes, status, reason,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def is_track_downloaded(track_id: int) -> bool:
    if not track_id:
        return False
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM downloads WHERE track_id = ? AND status = ? LIMIT 1",
            (track_id, STATUS_DOWNLOADED),
        ).fetchone()
        return row is not None


def is_release_seen(release_id: int) -> bool:
    """True if we have any record at all (any status) for this release — used by
    the watch-list feature to decide whether a release has already been evaluated
    (downloaded, or explicitly baselined as pre-existing catalogue)."""
    if not release_id:
        return False
    with _lock, _connect() as conn:
        row = conn.execute("SELECT 1 FROM downloads WHERE release_id = ? LIMIT 1", (release_id,)).fetchone()
        return row is not None


def mark_release_baseline(release_id: int, release_name: str, label: str, reason: str = "baseline (predates watch)") -> None:
    """Marks a release as 'seen' without downloading it — used when a watched
    label is checked for the first time and its existing back-catalogue (released
    before we started watching) shouldn't be mistaken for new releases."""
    record(
        track_id=0, release_id=release_id, store="", track_name="", artists="",
        release_name=release_name, label=label, genre="", subgenre="", bpm=0, key="",
        quality="", file_path="", status=STATUS_SKIPPED, reason=reason,
    )


def add_pending_release(release_id: int, label_url: str, release_name: str, expected_date: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO pending_releases
               (release_id, label_url, release_name, expected_date, first_seen_at)
               VALUES (?, ?, ?, ?, ?)""",
            (release_id, label_url, release_name, expected_date, datetime.now(timezone.utc).isoformat()),
        )


def get_due_pending(label_url: str) -> list[dict]:
    """Pre-releases we're tracking for this label whose release date has arrived."""
    today = datetime.now(timezone.utc).date().isoformat()
    with _lock, _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM pending_releases WHERE label_url = ? AND expected_date <= ?",
            (label_url, today),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_pending(label_url: str) -> list[dict]:
    with _lock, _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM pending_releases WHERE label_url = ? ORDER BY expected_date", (label_url,)
        ).fetchall()
        return [dict(r) for r in rows]


def remove_pending(release_id: int, label_url: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "DELETE FROM pending_releases WHERE release_id = ? AND label_url = ?", (release_id, label_url)
        )


def _find_audio_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    files = []
    for dirpath, _dirnames, filenames in __import__("os").walk(root):
        for fn in filenames:
            if fn.lower().endswith(AUDIO_EXTENSIONS):
                files.append(Path(dirpath) / fn)
    return files


def backfill_from_disk(downloads_dir: str) -> dict:
    """One-time catch-up: scan the downloads directory for files with an embedded
    BEATPORT_TRACK_ID that aren't in the history DB yet (files downloaded before
    history tracking existed), and record them using tags read straight from the
    file itself. Safe to call repeatedly — skips track_ids already recorded."""
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4

    root = Path(downloads_dir)
    files = _find_audio_files(root)
    added = 0
    skipped_existing = 0
    unreadable = 0

    with _lock:
        with _connect() as conn:
            existing_ids = {row[0] for row in conn.execute("SELECT DISTINCT track_id FROM downloads")}
            # Backfill file sizes for rows recorded before file_size_bytes existed —
            # matters directly for the volume-over-time stats, not just cosmetic.
            zero_size_rows = conn.execute(
                "SELECT id, file_path FROM downloads WHERE (file_size_bytes IS NULL OR file_size_bytes = 0) "
                "AND file_path != ''"
            ).fetchall()
            for row_id, file_path in zero_size_rows:
                try:
                    size = Path(file_path).stat().st_size
                except OSError:
                    continue
                if size:
                    conn.execute("UPDATE downloads SET file_size_bytes = ? WHERE id = ?", (size, row_id))

    for f in files:
        try:
            release_id, track_id = read_embedded_ids(str(f))
            if not track_id or track_id in existing_ids:
                skipped_existing += 1
                continue
            ext = f.suffix.lower()
            if ext == ".flac":
                audio = FLAC(str(f))
                get = lambda k: (audio.get(k) or [""])[0]  # noqa: E731
                genre, bpm_raw, key = get("GENRE"), get("BPM"), get("KEY")
                title, artist, album, label = get("TITLE"), get("ARTIST"), get("ALBUM"), get("LABEL")
            elif ext == ".m4a":
                audio = MP4(str(f))
                title = str((audio.get("\xa9nam") or [""])[0])
                artist = str((audio.get("\xa9ART") or [""])[0])
                album = str((audio.get("\xa9alb") or [""])[0])
                genre = str((audio.get("\xa9gen") or [""])[0])
                label, key, bpm_raw = "", "", ""
            else:
                continue
            bpm = int(bpm_raw) if str(bpm_raw).isdigit() else 0
            try:
                size_bytes = f.stat().st_size
            except OSError:
                size_bytes = 0
            record(
                track_id=track_id, release_id=release_id, store="", track_name=title,
                artists=artist, release_name=album, label=label, genre=genre, subgenre="",
                bpm=bpm, key=key, quality="", file_path=str(f), file_size_bytes=size_bytes,
                status=STATUS_DOWNLOADED, reason="backfilled",
            )
            existing_ids.add(track_id)
            added += 1
        except Exception:
            unreadable += 1
            continue

    return {"scanned_files": len(files), "added": added, "already_tracked": skipped_existing, "unreadable": unreadable}


def get_recent(limit: int = 50) -> list[dict]:
    with _lock, _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM downloads WHERE status = ? ORDER BY id DESC LIMIT ?", (STATUS_DOWNLOADED, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats() -> dict:
    with _lock, _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT genre, subgenre, artists, label, bpm, key, quality, downloaded_at, release_id, "
            "file_size_bytes FROM downloads WHERE status = ?",
            (STATUS_DOWNLOADED,),
        ).fetchall()

    genres: Counter[str] = Counter()
    subgenres: Counter[str] = Counter()
    artists: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    keys: Counter[str] = Counter()
    quality: Counter[str] = Counter()
    months: Counter[str] = Counter()
    bpm_buckets: Counter[str] = Counter()
    releases: set[int] = set()

    bytes_by_day: Counter[str] = Counter()
    bytes_by_week: Counter[str] = Counter()
    bytes_by_month: Counter[str] = Counter()
    bytes_by_year: Counter[str] = Counter()
    total_bytes = 0

    for row in rows:
        if row["genre"]:
            genres[row["genre"]] += 1
        if row["subgenre"]:
            subgenres[row["subgenre"]] += 1
        if row["label"]:
            labels[row["label"]] += 1
        if row["key"]:
            keys[row["key"]] += 1
        if row["quality"]:
            quality[row["quality"]] += 1
        if row["release_id"]:
            releases.add(row["release_id"])
        for a in (row["artists"] or "").split(", "):
            a = a.strip()
            if a:
                artists[a] += 1
        if row["downloaded_at"]:
            months[row["downloaded_at"][:7]] += 1

        size = row["file_size_bytes"] or 0
        total_bytes += size
        if row["downloaded_at"] and size:
            try:
                dt = datetime.fromisoformat(row["downloaded_at"])
                iso_year, iso_week, _ = dt.isocalendar()
                bytes_by_day[dt.date().isoformat()] += size
                bytes_by_week[f"{iso_year}-W{iso_week:02d}"] += size
                bytes_by_month[dt.strftime("%Y-%m")] += size
                bytes_by_year[str(dt.year)] += size
            except ValueError:
                pass

        bpm = row["bpm"] or 0
        if bpm > 0:
            bucket_start = (bpm // 10) * 10
            bpm_buckets[f"{bucket_start}-{bucket_start + 9}"] += 1

    def top(counter: Counter, n: int = 15) -> list[dict]:
        return [{"name": k, "count": v} for k, v in counter.most_common(n)]

    def by_bucket(counter: Counter, key_name: str) -> list[dict]:
        return sorted(({key_name: k, "bytes": v} for k, v in counter.items()), key=lambda x: x[key_name])

    return {
        "total_tracks": len(rows),
        "total_releases": len(releases),
        "total_labels": len(labels),
        "total_artists": len(artists),
        "total_bytes": total_bytes,
        "genres": top(genres, 20),
        "subgenres": top(subgenres, 20),
        "artists": top(artists, 20),
        "labels": top(labels, 20),
        "keys": top(keys, 24),
        "quality": top(quality, 5),
        "activity_by_month": sorted(({"month": k, "count": v} for k, v in months.items()), key=lambda x: x["month"]),
        "bpm_buckets": sorted(
            ({"range": k, "count": v} for k, v in bpm_buckets.items()),
            key=lambda x: int(x["range"].split("-")[0]),
        ),
        "bytes_by_day": by_bucket(bytes_by_day, "day"),
        "bytes_by_week": by_bucket(bytes_by_week, "week"),
        "bytes_by_month": by_bucket(bytes_by_month, "month"),
        "bytes_by_year": by_bucket(bytes_by_year, "year"),
    }


def verify_library() -> dict:
    """Read-only check of every 'downloaded' row's file_path against disk. This
    is informational only — it never deletes anything on its own. Users who
    move files elsewhere after downloading will legitimately see many "missing"
    entries here; that's expected for that workflow (dedup should still treat
    those tracks as downloaded), not a sign of corruption. Users who keep a
    stable library layout can use the count to spot real problems."""
    with _lock, _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, track_id, track_name, artists, release_name, file_path "
            "FROM downloads WHERE status = ?",
            (STATUS_DOWNLOADED,),
        ).fetchall()

    missing = []
    ok_count = 0
    no_path_count = 0
    for row in rows:
        if not row["file_path"]:
            no_path_count += 1
            continue
        if Path(row["file_path"]).exists():
            ok_count += 1
        else:
            missing.append({
                "id": row["id"], "track_id": row["track_id"], "track_name": row["track_name"],
                "artists": row["artists"], "release_name": row["release_name"], "file_path": row["file_path"],
            })

    return {
        "total_checked": len(rows),
        "ok": ok_count,
        "missing": len(missing),
        "no_path_recorded": no_path_count,
        "missing_sample": missing[:100],
    }


def remove_missing_entries() -> int:
    """Opt-in cleanup for verify_library()'s 'missing' set — deletes downloaded
    rows whose file_path no longer exists. Never called automatically; a user
    on a stable-library workflow explicitly requests this after reviewing the
    verify report. Never touches rows with an empty file_path (those predate
    file-size tracking and shouldn't be judged missing just because we never
    recorded where they went)."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, file_path FROM downloads WHERE status = ? AND file_path != ''",
            (STATUS_DOWNLOADED,),
        ).fetchall()
        missing_ids = [row[0] for row in rows if not Path(row[1]).exists()]
        if missing_ids:
            placeholders = ",".join("?" * len(missing_ids))
            conn.execute(f"DELETE FROM downloads WHERE id IN ({placeholders})", missing_ids)
        return len(missing_ids)


def clear_all() -> int:
    """Wipes the entire download history — for users who move files elsewhere
    after downloading and want a clean dedup/stats slate rather than relying on
    file-existence checks that will never match their workflow. Does not touch
    pending_releases (watch-list pre-release tracking) since that's unrelated
    to what's already been downloaded."""
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM downloads")
        conn.commit()
        return cur.rowcount
