"""Spectral transcode detection — is this "lossless" file really lossless?

A lossy encoder throws away everything above a cutoff frequency and cannot put it
back. Re-encoding the result to FLAC/WAV rebuilds a lossless *container* around
permanently lossy audio: the extension, the bitrate and the file size all look
right, and only the spectrum still knows. This module measures the spectrum.

The method is the one that survived a merchant dispute (see the round-trip control
in `analyse_with_control`): decode to raw PCM, take a long-term average power
spectrum by FFT (16384-point Hann, 50% overlap, digital silence skipped),
normalise so the loudest bin is 0 dB, then report

  * `cutoff_hz`  — the highest frequency still within CUTOFF_DROP_DB of the
                   1–6 kHz mid-band reference,
  * `wall_db`    — how sharply energy falls across that cutoff. Mastering rolls
                   off gently; an encoder's brick wall does not.
  * `above_db`   — the mean level above the cutoff. This is the metric that
                   settles arguments: a genuine 16-bit file still has dither and
                   noise up there (about -68 dB), a decoded MP3 has literally
                   nothing (about -107 dB).

Everything here is a pure function over a file path so it can be tested against
known-answer controls, which is the only reason to trust any of it.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

AUDIO_EXTENSIONS = (".flac", ".wav", ".aiff", ".aif", ".alac", ".m4a", ".ape", ".wv", ".tta")
"""Formats worth checking. Deliberately excludes .mp3/.aac/.ogg — those are *meant*
to be lossy, so reporting a wall in one is noise, not a finding."""

FFT_SIZE = 16384
HOP = FFT_SIZE // 2  # 50% overlap
SILENCE_DBFS = -70.0  # frames quieter than this are digital silence / fades
REF_LOW_HZ, REF_HIGH_HZ = 1000.0, 6000.0  # mid-band reference, always full of music
CUTOFF_DROP_DB = 45.0  # how far under the mid-band still counts as "energy present"
WALL_SPAN_HZ = 600.0  # measured either side of the cutoff
MAX_ANALYSIS_SECONDS = 300.0  # five minutes of audio is plenty; keeps a 2-hour mix quick

# Cutoff -> the encoder setting that produces it, at 44.1/48 kHz. Ranges are the
# measured round-trip controls, not folklore.
_BITRATE_TABLE = [
    (16500.0, "128 kbps or lower"),
    (18000.0, "160 kbps"),
    (19000.0, "192 kbps"),
    (19800.0, "256 kbps / MP3 V2"),
    (20400.0, "320 kbps"),
    (21200.0, "MP3 V0 / AAC 256"),
]


class FFmpegMissing(RuntimeError):
    """No decoder available. Raised with the same wording the UI shows."""


def _beside_executable() -> str | None:
    """An ffmpeg sitting next to the running program.

    This is how the Windows build gets one: the release zip ships `ffmpeg.exe`
    beside `bpdl-web.exe`. Putting it there rather than inside the one-file exe
    keeps a ~100 MB binary out of the temp-directory unpack that happens on every
    single launch — and leaves it somewhere a user can see, replace or delete.
    """
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).parent)
    roots.append(Path(__file__).resolve().parent.parent)
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return str(candidate)
    return None


def ffmpeg_exe() -> str:
    """The ffmpeg to decode with, in order of preference.

    An explicit override first, then one shipped beside the program, then whatever
    is on PATH, and finally the copy imageio-ffmpeg downloads at install time. The
    override exists because a user with their own build — one with codecs a stock
    binary lacks — should not have to fight the bundled one.
    """
    override = os.environ.get("BPDL_FFMPEG", "").strip()
    if override:
        if Path(override).is_file():
            return override
        raise FFmpegMissing(f"BPDL_FFMPEG points at {override}, which is not a file")

    beside = _beside_executable()
    if beside:
        return beside

    found = shutil.which("ffmpeg")
    if found:
        return found

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise FFmpegMissing(
            "ffmpeg not found. Put ffmpeg next to the program, install it on PATH, "
            "or set BPDL_FFMPEG to its full path."
        )


def _no_window() -> dict:
    """Keep ffmpeg from flashing a console window on Windows.

    A folder scan runs ffmpeg once per file, so without this a 200-track album is
    200 black windows popping over whatever the user is doing.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def ffmpeg_available() -> bool:
    try:
        ffmpeg_exe()
        return True
    except FFmpegMissing:
        return False


# ---- decoding ------------------------------------------------------------------------------

LOSSY_CODEC_HINTS = ("mp4a", "aac", "mp3", "vorbis", "opus")


@dataclass
class StreamInfo:
    sample_rate: int = 0
    channels: int = 0
    declared_bits: int = 0
    duration: float = 0.0
    codec: str = ""
    lossy_format: bool = False


def probe(path: str) -> StreamInfo:
    """Stream properties from mutagen where it knows the format (it is already a
    dependency and it reads the real FLAC STREAMINFO), falling back to parsing
    ffmpeg's own banner — imageio-ffmpeg ships no ffprobe, so this cannot rely on one."""
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path)
        si = getattr(mf, "info", None)
        if si is not None and getattr(si, "sample_rate", 0):
            # `.m4a` says nothing about whether the audio inside is lossless: the same
            # extension carries ALAC and AAC, and bp-dl's own medium quality writes AAC.
            # Reporting a brick wall in a file that is *meant* to be lossy is noise, so
            # the codec, not the extension, decides whether it gets analysed at all.
            codec = str(getattr(si, "codec", "") or type(mf).__name__).lower()
            return StreamInfo(
                sample_rate=int(getattr(si, "sample_rate", 0) or 0),
                channels=int(getattr(si, "channels", 0) or 0),
                declared_bits=int(getattr(si, "bits_per_sample", 0) or 0),
                duration=float(getattr(si, "length", 0.0) or 0.0),
                codec=codec,
                lossy_format=any(h in codec for h in LOSSY_CODEC_HINTS),
            )
    except Exception:
        pass
    return _probe_via_ffmpeg(path)


def _probe_via_ffmpeg(path: str) -> StreamInfo:
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", path],
        capture_output=True,
        text=True,
        errors="replace",
        **_no_window(),
    )
    info = StreamInfo()
    for line in proc.stderr.splitlines():
        line = line.strip()
        if not line.startswith("Stream #") or "Audio:" not in line:
            continue
        body = line.split("Audio:", 1)[1]
        parts = [p.strip() for p in body.split(",")]
        info.codec = parts[0].split()[0] if parts else ""
        for p in parts:
            if p.endswith("Hz") and p[:-2].strip().isdigit():
                info.sample_rate = int(p[:-2].strip())
            elif p == "mono":
                info.channels = 1
            elif p == "stereo":
                info.channels = 2
            elif p.startswith("s") and p[1:].split()[0].isdigit():
                info.declared_bits = int(p[1:].split()[0])
        break
    return info


_TAG_KEYS = {
    "artist": ("artist", "albumartist", "\xa9ART", "aART", "TPE1"),
    "title": ("title", "\xa9nam", "TIT2"),
    "album": ("album", "\xa9alb", "TALB"),
    "date": ("date", "originaldate", "year", "\xa9day", "TDRC", "TYER"),
}


def read_tags(path: str) -> dict[str, str]:
    """Artist / title / album / date, whatever the container calls them.

    A verdict on "03. Untitled.flac" is not much use when the point is to go back to
    a seller or refile a release, so every row carries who and what it is. Vorbis,
    MP4 and ID3 spell all four differently; missing tags are simply blank.
    """
    out = {k: "" for k in _TAG_KEYS}
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path)
        if mf is None or not getattr(mf, "tags", None):
            return out
        for field, keys in _TAG_KEYS.items():
            for key in keys:
                try:
                    val = mf.tags.get(key)
                except Exception:
                    val = None
                if not val:
                    continue
                if isinstance(val, (list, tuple)):
                    val = val[0] if val else ""
                text = str(val).strip()
                if text:
                    out[field] = text[:200]
                    break
    except Exception:
        pass
    # A date tag is routinely a full ISO timestamp; the year is what anyone reads.
    if out["date"][:4].isdigit():
        out["date"] = out["date"][:4]
    return out


def _decode_chunks(path: str, sample_rate: int, channels: int, max_seconds: float):
    """Stream the file through ffmpeg as 32-bit ints, a chunk at a time.

    Signed 32-bit rather than float is deliberate: it keeps the original integers
    exact, so the same single decode pass yields both the spectrum and the real
    bit depth (a "24-bit" file padded up from a 16-bit master has eight dead low
    bits, and no amount of FFT would show that).
    """
    limit = ["-t", f"{max_seconds:.3f}"] if max_seconds > 0 else []
    cmd = [
        ffmpeg_exe(), "-v", "error", "-nostdin",
        *limit, "-i", path,
        "-map", "0:a:0",
        "-f", "s32le", "-acodec", "pcm_s32le",
        "-",
    ]
    frame_bytes = 4 * max(channels, 1)
    read_size = frame_bytes * 65536
    # stderr goes to a temp file, not a pipe: nothing reads it until the decode is
    # finished, and a file that makes ffmpeg complain on every frame would otherwise
    # fill the pipe buffer and deadlock the read loop.
    errfile = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile, **_no_window())
    tail = b""
    try:
        while True:
            buf = proc.stdout.read(read_size)
            if not buf:
                break
            buf = tail + buf
            usable = len(buf) - (len(buf) % frame_bytes)
            tail = buf[usable:]
            if usable:
                yield np.frombuffer(buf[:usable], dtype="<i4").reshape(-1, max(channels, 1))
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()
        errfile.seek(0)
        err = errfile.read()
        errfile.close()
        if proc.returncode not in (0, None) and err:
            raise RuntimeError(err.decode("utf-8", "replace").strip().splitlines()[-1][:200])


# ---- analysis ------------------------------------------------------------------------------

@dataclass
class Analysis:
    path: str = ""
    name: str = ""
    ok: bool = True
    error: str = ""
    sample_rate: int = 0
    channels: int = 0
    declared_bits: int = 0
    effective_bits: int = 0
    duration: float = 0.0
    frames: int = 0
    cutoff_hz: float = 0.0
    nyquist_hz: float = 0.0
    wall_db: float = 0.0
    above_db: float = 0.0
    top_band_db: float = 0.0
    side_ratio_db: float = 0.0
    codec: str = ""
    artist: str = ""
    title: str = ""
    album: str = ""
    date: str = ""
    verdict: str = "unknown"  # clean | suspect | lossy | upsampled | padded | lossy_format | unreadable
    confidence: int = 0
    estimated_source: str = ""
    reasons: list[str] = field(default_factory=list)
    bands: list[tuple[float, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["bands"] = [[round(f), round(v, 1)] for f, v in self.bands]
        return d


def _hann(n: int) -> np.ndarray:
    return np.hanning(n).astype(np.float64)


def _smooth(a: np.ndarray, width: int) -> np.ndarray:
    """Moving average with the ends held, not zero-padded.

    numpy's "same" convolution pads with zeros, and these are dB values where zero
    is the *loudest* possible bin — so a plain moving average makes the top of every
    spectrum climb towards 0 dB and hides the very brick wall this is looking for.
    """
    if width < 2:
        return a
    pad = width // 2
    padded = np.concatenate([np.full(pad, a[0]), a, np.full(pad, a[-1])])
    kernel = np.ones(width) / width
    return np.convolve(padded, kernel, mode="valid")[:len(a)]


def analyse(path: str, max_seconds: float = MAX_ANALYSIS_SECONDS) -> Analysis:
    """Measure one file. Never raises for a bad file — a folder scan must not stop
    on the one track with a broken header."""
    res = Analysis(path=str(path), name=Path(path).name)
    try:
        info = probe(str(path))
        if not info.sample_rate:
            raise RuntimeError("could not read stream info")
        res.sample_rate = info.sample_rate
        res.channels = info.channels or 2
        res.declared_bits = info.declared_bits
        res.duration = info.duration
        res.codec = info.codec
        res.nyquist_hz = info.sample_rate / 2.0
        for field, value in read_tags(str(path)).items():
            setattr(res, field, value)
        if info.lossy_format:
            res.verdict = "lossy_format"
            res.confidence = 100
            res.reasons = [f"{info.codec} — a lossy format by design, so there is nothing to fake here"]
            return res

        window = _hann(FFT_SIZE)
        win_power = float(np.sum(window**2))
        mid_sum = np.zeros(FFT_SIZE // 2 + 1)
        side_sum = np.zeros(FFT_SIZE // 2 + 1)
        used = 0
        used_bits_mask = np.int64(0)
        pending = np.zeros((0, res.channels), dtype=np.int32)
        # Silence is measured against full scale, so the threshold is a constant
        # number of counts rather than something that drifts with the material.
        silence_amp = (10.0 ** (SILENCE_DBFS / 20.0)) * 2147483648.0

        for chunk in _decode_chunks(str(path), info.sample_rate, res.channels, max_seconds):
            used_bits_mask |= np.bitwise_or.reduce(np.abs(chunk.astype(np.int64)).ravel())
            pending = np.concatenate([pending, chunk]) if len(pending) else chunk
            pos = 0
            while pos + FFT_SIZE <= len(pending):
                frame = pending[pos:pos + FFT_SIZE].astype(np.float64)
                pos += HOP
                if frame.shape[1] >= 2:
                    mid = (frame[:, 0] + frame[:, 1]) * 0.5
                    side = (frame[:, 0] - frame[:, 1]) * 0.5
                else:
                    mid = frame[:, 0]
                    side = None
                if float(np.sqrt(np.mean(mid**2))) < silence_amp:
                    continue
                mid_sum += np.abs(np.fft.rfft(mid * window)) ** 2
                if side is not None:
                    side_sum += np.abs(np.fft.rfft(side * window)) ** 2
                used += 1
            pending = pending[pos:]

        if used < 4:
            raise RuntimeError("not enough non-silent audio to analyse")
        res.frames = used

        freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / info.sample_rate)
        mid_db = 10.0 * np.log10(np.maximum(mid_sum / (used * win_power), 1e-30))
        mid_db -= float(np.max(mid_db))
        smooth = _smooth(mid_db, 9)

        res.effective_bits = _effective_bits(int(used_bits_mask))
        _measure(res, freqs, smooth, mid_db)
        if np.any(side_sum):
            res.side_ratio_db = _side_ratio(freqs, mid_sum, side_sum, res.cutoff_hz)
        res.bands = _band_table(freqs, smooth)
        _verdict(res)
    except FFmpegMissing:
        raise
    except Exception as e:  # a corrupt file is a result, not a crash
        res.ok = False
        res.error = str(e)[:300] or e.__class__.__name__
        res.verdict = "unreadable"
    return res


def _effective_bits(mask: int) -> int:
    """How many bits the samples actually use. ffmpeg left-aligns into 32 bits, so
    a genuine 16-bit master leaves the low 16 always zero, and a 24-bit file that
    was really 16-bit upscaled leaves the low 8 always zero on top of that."""
    if mask <= 0:
        return 0
    trailing = 0
    while mask & 1 == 0:
        mask >>= 1
        trailing += 1
    return max(0, 32 - trailing)


def _band_at(freqs: np.ndarray, spec: np.ndarray, lo: float, hi: float) -> float:
    sel = (freqs >= lo) & (freqs <= hi)
    if not np.any(sel):
        return float("-inf")
    return float(np.mean(spec[sel]))


def _measure(res: Analysis, freqs: np.ndarray, smooth: np.ndarray, raw: np.ndarray) -> None:
    ref = _band_at(freqs, smooth, REF_LOW_HZ, REF_HIGH_HZ)
    threshold = ref - CUTOFF_DROP_DB
    above_ref = np.where((smooth >= threshold) & (freqs > REF_HIGH_HZ))[0]
    res.cutoff_hz = float(freqs[above_ref[-1]]) if len(above_ref) else REF_HIGH_HZ

    below = _band_at(freqs, smooth, max(0.0, res.cutoff_hz - WALL_SPAN_HZ), res.cutoff_hz)
    over = _band_at(freqs, smooth, res.cutoff_hz, res.cutoff_hz + WALL_SPAN_HZ)
    res.wall_db = round(below - over, 1) if math.isfinite(over) else 0.0

    # Everything above the cutoff, measured on the *unsmoothed* spectrum: smoothing
    # would drag the brick wall's shoulder into the empty band and soften the number
    # that matters most.
    top_start = min(res.cutoff_hz + 500.0, res.nyquist_hz - 200.0)
    res.above_db = round(_band_at(freqs, raw, top_start, res.nyquist_hz - 100.0), 1)
    res.top_band_db = round(_band_at(freqs, raw, res.nyquist_hz * 0.95, res.nyquist_hz), 1)
    res.wall_db = round(res.wall_db, 1)
    res.cutoff_hz = round(res.cutoff_hz, 1)


def _side_ratio(freqs: np.ndarray, mid_sum: np.ndarray, side_sum: np.ndarray, cutoff: float) -> float:
    """Side-channel energy just under the cutoff, relative to mid. Joint-stereo
    encoders collapse the top of the stereo image to mono, so a near-empty side
    channel up there is a second, independent sign of a lossy ancestor."""
    lo, hi = max(cutoff * 0.75, 8000.0), max(cutoff, 8100.0)
    sel = (freqs >= lo) & (freqs <= hi)
    if not np.any(sel):
        return 0.0
    m = float(np.sum(mid_sum[sel]))
    s = float(np.sum(side_sum[sel]))
    if m <= 0:
        return 0.0
    return round(10.0 * math.log10(max(s, 1e-30) / m), 1)


def _band_table(freqs: np.ndarray, smooth: np.ndarray) -> list[tuple[float, float]]:
    edges = [0, 1000, 2000, 4000, 8000, 12000, 14000, 16000, 17000, 18000, 19000,
             19500, 20000, 20500, 21000, 22050, 24000, 32000, 48000, 96000]
    out: list[tuple[float, float]] = []
    for lo, hi in zip(edges, edges[1:]):
        if lo >= freqs[-1]:
            break
        v = _band_at(freqs, smooth, lo, min(hi, freqs[-1]))
        if math.isfinite(v):
            out.append((float(lo), float(v)))
    return out


def estimate_source(cutoff: float) -> str:
    for edge, label in _BITRATE_TABLE:
        if cutoff < edge:
            return label
    return "high-bitrate lossy"


def _verdict(res: Analysis) -> None:
    """Turn the numbers into a call, and say which numbers made it.

    The bar for `lossy` is deliberately all three of: a cutoff well under Nyquist,
    a sharp wall at it, and a dead band above it. A mastering engineer can produce
    any one of those; an encoder produces all three together.
    """
    reasons: list[str] = []
    nyq = res.nyquist_hz
    headroom = nyq - res.cutoff_hz

    if res.declared_bits >= 24 and 0 < res.effective_bits <= 16:
        res.verdict = "padded"
        res.confidence = 95
        res.reasons = [f"declared {res.declared_bits}-bit but only {res.effective_bits} bits are ever used "
                       f"— padded up from a {res.effective_bits}-bit master"]
        return

    if res.sample_rate > 48000 and res.cutoff_hz < 22000:
        res.verdict = "upsampled"
        res.confidence = 90
        res.reasons = [f"{res.sample_rate/1000:.1f} kHz file with nothing above "
                       f"{res.cutoff_hz/1000:.1f} kHz — upsampled, not a high-resolution master"]
        return

    dead_above = res.above_db <= -90.0
    sharp_wall = res.wall_db >= 20.0
    low_cutoff = headroom >= 900.0

    if low_cutoff:
        reasons.append(f"cuts off at {res.cutoff_hz/1000:.1f} kHz, {headroom/1000:.1f} kHz below Nyquist")
    if sharp_wall:
        reasons.append(f"{res.wall_db:.0f} dB brick wall across the cutoff")
    if dead_above:
        reasons.append(f"only {res.above_db:.0f} dB above it — below the dither floor any real "
                       "16-bit master leaves behind")
    if res.side_ratio_db <= -40.0 and low_cutoff:
        reasons.append(f"stereo side channel {res.side_ratio_db:.0f} dB under mid near the cutoff "
                       "— joint-stereo collapse")

    score = (30 if low_cutoff else 0) + (30 if sharp_wall else 0) + (35 if dead_above else 0)
    if res.side_ratio_db <= -40.0 and low_cutoff:
        score += 5

    if low_cutoff and sharp_wall and dead_above:
        res.verdict = "lossy"
        res.estimated_source = estimate_source(res.cutoff_hz)
        res.confidence = min(99, score)
    elif score >= 50:
        res.verdict = "suspect"
        res.estimated_source = estimate_source(res.cutoff_hz)
        res.confidence = score
    else:
        res.verdict = "clean"
        res.confidence = max(60, 100 - score)
        if not reasons:
            reasons.append(f"full spectrum to {res.cutoff_hz/1000:.1f} kHz with a natural noise "
                           f"floor above it ({res.above_db:.0f} dB)")
    res.reasons = reasons


# ---- folder scan ---------------------------------------------------------------------------

QUARANTINE_DIRNAME = "_transcode-quarantine"
RESTORE_LOG = "restore.tsv"

VERDICT_ORDER = {"lossy": 0, "padded": 1, "upsampled": 2, "suspect": 3,
                 "unreadable": 4, "clean": 5, "lossy_format": 6}


def find_audio_files(root: Path, recursive: bool = True) -> list[Path]:
    """Every lossless-claiming file under `root`, minus anything already quarantined —
    a second scan must not re-report the files the first one pulled out."""
    if not root.is_dir():
        return []
    walker = root.rglob("*") if recursive else root.glob("*")
    out = []
    for p in walker:
        if p.suffix.lower() not in AUDIO_EXTENSIONS or not p.is_file():
            continue
        if QUARANTINE_DIRNAME in p.parts:
            continue
        out.append(p)
    return sorted(out)


def scan_folder(
    root: str,
    max_seconds: float = MAX_ANALYSIS_SECONDS,
    recursive: bool = True,
    on_progress=None,
    should_stop=None,
) -> dict:
    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        raise NotADirectoryError(f"{root} is not a folder")
    files = find_audio_files(root_path, recursive)
    results: list[Analysis] = []
    stopped = False
    for i, f in enumerate(files, 1):
        if should_stop and should_stop():
            stopped = True
            break
        if on_progress:
            on_progress(i, len(files), str(f))
        results.append(analyse(str(f), max_seconds=max_seconds))
    results.sort(key=lambda a: (VERDICT_ORDER.get(a.verdict, 9), -a.confidence, a.name))
    counts: dict[str, int] = {}
    for a in results:
        counts[a.verdict] = counts.get(a.verdict, 0) + 1
    return {
        "root": str(root_path),
        "total_files": len(files),
        "scanned": len(results),
        "stopped": stopped,
        "counts": counts,
        "results": [a.to_dict() for a in results],
    }


def _under(root: Path, p: Path) -> bool:
    """Is `p` inside `root`?

    Windows compares paths case-insensitively and `relative_to` does not, so
    `C:\\Music` and `c:\\music` are the same folder to the filesystem and two
    different ones to pathlib. On a case-insensitive platform the check is made on
    the normalised case, or a delete refuses a file the user can plainly see.
    """
    try:
        p.relative_to(root)
        return True
    except ValueError:
        pass
    return os.name == "nt" and _under_ci(str(root), str(p))


def _under_ci(root: str, p: str) -> bool:
    """The case-insensitive form, kept separate so it can be tested on any platform
    without patching `os.name` — which pathlib reads at import and does not survive
    being changed underneath it."""
    root = root.replace("/", "\\").rstrip("\\").lower()
    p = p.replace("/", "\\").lower()
    return p == root or p.startswith(root + "\\")


def quarantine_files(paths: list[str], root: str, quarantine_dir: str = "") -> dict:
    """Move suspect files out of the library into a holding folder, keeping their
    folder structure and writing a restore log beside them.

    Default location is inside the scanned folder, which is the point: a move within
    one filesystem is instant, while a quarantine on another drive would copy every
    gigabyte. Nothing is ever deleted here — that is a separate, explicit act.
    """
    root_path = Path(root).expanduser().resolve()
    qdir = Path(quarantine_dir).expanduser() if quarantine_dir else root_path / QUARANTINE_DIRNAME
    qdir.mkdir(parents=True, exist_ok=True)
    log_path = qdir / RESTORE_LOG
    moved, failed, skipped = 0, [], 0
    lines = []
    for raw in paths:
        src = Path(raw).expanduser().resolve()
        if not _under(root_path, src):
            failed.append({"path": raw, "error": "outside the scanned folder"})
            continue
        if not src.is_file():
            skipped += 1
            continue
        rel = src.relative_to(root_path)
        dest = qdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest = _unique(dest)
        try:
            shutil.move(str(src), str(dest))
            lines.append(f"{dest}\t{src}")
            moved += 1
        except Exception as e:
            failed.append({"path": raw, "error": str(e)[:200]})
    if lines:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return {"moved": moved, "skipped": skipped, "failed": failed, "quarantine_dir": str(qdir)}


def _unique(dest: Path) -> Path:
    """Never overwrite in the quarantine folder: two different releases can hold a
    track of the same name, and the second one arriving must not erase the first."""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for n in range(2, 1000):
        cand = dest.with_name(f"{stem} ({n}){suffix}")
        if not cand.exists():
            return cand
    return dest.with_name(f"{stem} ({os.getpid()}){suffix}")


def restore_quarantined(quarantine_dir: str) -> dict:
    """Put everything back where it came from, using the log written at quarantine
    time, and clear the log for the entries that made it home."""
    qdir = Path(quarantine_dir).expanduser()
    log_path = qdir / RESTORE_LOG
    if not log_path.is_file():
        return {"restored": 0, "failed": [{"path": str(log_path), "error": "no restore log"}]}
    restored, failed, remaining = 0, [], []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        held, original = line.split("\t", 1)
        src, dest = Path(held), Path(original)
        if not src.is_file():
            continue  # already moved or deleted by hand — drop the line
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(_unique(dest)))
            restored += 1
        except Exception as e:
            failed.append({"path": held, "error": str(e)[:200]})
            remaining.append(line)
    if remaining:
        log_path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
    else:
        log_path.unlink(missing_ok=True)
    return {"restored": restored, "failed": failed}


def delete_files(paths: list[str], root: str) -> dict:
    """Permanent. Confined to the scanned folder so a mistyped path in a request
    can never reach anything else on the machine."""
    root_path = Path(root).expanduser().resolve()
    deleted, failed, skipped = 0, [], 0
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if not _under(root_path, p):
            failed.append({"path": raw, "error": "outside the scanned folder"})
            continue
        if not p.is_file():
            skipped += 1
            continue
        try:
            p.unlink()
            deleted += 1
        except Exception as e:
            failed.append({"path": raw, "error": str(e)[:200]})
    return {"deleted": deleted, "skipped": skipped, "failed": failed}


def export_spectrograms(results: list[dict], cache_dir: str, dest: str,
                        on_progress=None) -> dict:
    """Render each file's spectrogram and save it under `dest` as a readable name.

    The cache is keyed on a hash, which is right for serving and useless for
    keeping: a folder of `a3f9c2….png` is not evidence anyone can hand over. These
    come out as "verdict - artist - title.png" so the picture still means something
    a year later, in a mail to a seller, next to the text report.
    """
    out_dir = Path(dest).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    saved, failed = 0, []
    for i, r in enumerate(results, 1):
        if on_progress:
            on_progress(i, len(results), r.get("name", ""))
        try:
            png = spectrogram_png(r["path"], cache_dir)
            # Album is in the name because the same artist and title legitimately
            # appear on two different releases — an original and a compilation cut —
            # and two files called "lossy - Artist - Track.png" are no use as evidence.
            name = " - ".join(x for x in (r.get("verdict", ""), r.get("artist", ""),
                                          r.get("album", ""),
                                          r.get("title") or r.get("name", "")) if x)
            target = _unique(out_dir / (_safe_name(name) + ".png"))
            shutil.copyfile(png, target)
            saved += 1
        except Exception as e:
            failed.append({"path": r.get("path", ""), "error": str(e)[:200]})
    return {"saved": saved, "failed": failed, "dir": str(out_dir)}


def _safe_name(name: str) -> str:
    """A filename both Windows and Linux will accept.

    Track titles routinely carry `?`, `:` and `/` — a remix credit alone can hold
    two of them — and on Windows every one of those is illegal in a filename."""
    cleaned = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)
    return cleaned.strip(" .")[:120] or "spectrogram"


def format_report(scan: dict) -> str:
    """A plain-text report of a scan, including the per-band table, so a finding can
    be handed to a seller or a label as numbers rather than a screenshot."""
    from datetime import datetime

    lines = [
        "TRANSCODE / FAKE-LOSSLESS SCAN",
        f"Folder : {scan['root']}",
        f"Date   : {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Files  : {scan['scanned']} of {scan['total_files']} analysed",
        "Method : long-term average spectrum, 16384-point Hann FFT, 50% overlap,",
        "         digital silence skipped, normalised to the loudest bin.",
        "",
        "SUMMARY",
    ]
    for verdict in sorted(scan["counts"], key=lambda v: VERDICT_ORDER.get(v, 9)):
        lines.append(f"  {verdict:<10} {scan['counts'][verdict]}")
    lines.append("")
    for r in scan["results"]:
        if r["verdict"] in ("clean", "lossy_format"):
            continue
        lines.append("-" * 78)
        lines.append(f"{r['verdict'].upper()}  ({r['confidence']}% confidence)  {r['name']}")
        lines.append(f"  {r['path']}")
        if r["error"]:
            lines.append(f"  error: {r['error']}")
            continue
        lines.append(
            f"  {r['sample_rate']} Hz  {r['channels']}ch  "
            f"{r['declared_bits']}-bit declared / {r['effective_bits']}-bit used"
        )
        lines.append(
            f"  cutoff {r['cutoff_hz']:.0f} Hz   wall {r['wall_db']:.1f} dB   "
            f"above cutoff {r['above_db']:.1f} dB   side/mid {r['side_ratio_db']:.1f} dB"
        )
        if r["estimated_source"]:
            lines.append(f"  consistent with: {r['estimated_source']}")
        for reason in r["reasons"]:
            lines.append(f"  - {reason}")
        if r["bands"]:
            lines.append("  band (Hz)      mean dB")
            for lo, v in r["bands"]:
                lines.append(f"  {lo:>8.0f}+     {v:>7.1f}")
    lines.append("-" * 78)
    lines.append("")
    lines.append("A lossy verdict needs all three of: a cutoff well below Nyquist, a sharp")
    lines.append("wall at it, and a dead band above it. Mastering can produce any one of")
    lines.append("those on its own; only an encoder produces all three together.")
    return "\n".join(lines) + "\n"


# ---- spectrogram ---------------------------------------------------------------------------

SPECTROGRAM_SIZE = (1024, 512)


def _pow2_height(h: int) -> int:
    """Round a spectrogram height down to a power of two.

    ⚠ This is not cosmetic. showspectrumpic derives its FFT size from the image
    height, then draws one frequency bin per pixel row — so a height that is not a
    power of two leaves the picture showing only the BOTTOM of the spectrum, with no
    warning and a frequency axis that looks perfectly plausible. At 520 px a 44.1 kHz
    file rendered as 0–11 kHz: every brick wall was above the top edge of the image,
    and a transcode looked flawless.
    """
    h = max(128, min(int(h), 2048))
    return 1 << (h.bit_length() - 1)


def spectrogram_png(path: str, cache_dir: str, size: tuple[int, int] = SPECTROGRAM_SIZE) -> Path:
    """Render a Spek-style spectrogram for one file, cached on disk.

    Same picture Spek draws, from the same underlying transform: linear frequency
    axis so a brick wall reads as a straight horizontal line, log magnitude, and the
    axes and colour bar drawn in so the image can be read on its own. The numbers
    above are the argument; this is how you spot which file to look at.

    Cached under the file's path *and* mtime, so an edited or replaced file renders
    again rather than serving a stale picture of the old audio.
    """
    import hashlib

    src = Path(path)
    stat = src.stat()
    width, height = int(size[0]), _pow2_height(size[1])
    key = hashlib.sha1(f"{src}|{stat.st_mtime_ns}|{width}x{height}".encode()).hexdigest()[:20]
    out = Path(cache_dir) / f"{key}.png"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.png")
    filt = (f"showspectrumpic=s={width}x{height}:mode=combined:color=intensity"
            f":scale=log:fscale=lin:legend=1")
    proc = subprocess.run(
        [ffmpeg_exe(), "-v", "error", "-nostdin", "-y", "-i", str(src),
         "-lavfi", filt, "-frames:v", "1", str(tmp)],
        capture_output=True,
        **_no_window(),
    )
    if proc.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(detail[-1][:200] if detail else "spectrogram render failed")
    tmp.replace(out)
    return out
