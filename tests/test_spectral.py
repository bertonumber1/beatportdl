"""The fake-lossless detector, checked against known answers.

The whole feature is a claim about other people's files, so the tests that matter
are the controls: encode a file we *know* is full-band down to MP3/AAC, decode it
back to FLAC, and require the analyser to catch it — and require it to leave the
original alone. Those two run only where ffmpeg exists; everything else is pure
maths and file moves and runs anywhere.
"""
from __future__ import annotations

import os
import re
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from bpdl import spectral
from bpdl.spectral import Analysis

HAS_FFMPEG = spectral.ffmpeg_available()
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


# ---- pure maths -------------------------------------------------------------------------

def test_smooth_does_not_lift_the_top_of_the_spectrum():
    """Regression: numpy's "same" convolution zero-pads, and these are dB values where
    0 is the loudest bin — so a naive moving average made every spectrum climb towards
    0 dB at Nyquist and hid the brick wall the detector exists to find."""
    spec = np.concatenate([np.full(200, -20.0), np.full(200, -110.0)])
    out = spectral._smooth(spec, 9)
    assert out[-1] < -100
    assert out[0] > -30


def test_effective_bits_sees_through_padding():
    assert spectral._effective_bits(0xFFFF0000) == 16  # a real 16-bit master
    assert spectral._effective_bits(0xFFFFFF00) == 24
    assert spectral._effective_bits(0) == 0


def test_estimate_source_tracks_the_cutoff():
    assert "128" in spectral.estimate_source(15000)
    assert "192" in spectral.estimate_source(18800)
    assert "320" in spectral.estimate_source(20150)


def _mk(**kw) -> Analysis:
    base = dict(sample_rate=44100, nyquist_hz=22050.0, declared_bits=16,
                effective_bits=16, cutoff_hz=22050.0, wall_db=1.0, above_db=-65.0)
    base.update(kw)
    return Analysis(**base)


def test_verdict_needs_all_three_signs_to_call_it_lossy():
    a = _mk(cutoff_hz=20100.0, wall_db=38.0, above_db=-107.0)
    spectral._verdict(a)
    assert a.verdict == "lossy"
    assert "320" in a.estimated_source


def test_a_mastering_lowpass_alone_is_not_a_transcode():
    """A gentle roll-off with a live noise floor above it is what a mastering engineer
    leaves behind, and must not be called a fake."""
    a = _mk(cutoff_hz=20000.0, wall_db=6.0, above_db=-70.0)
    spectral._verdict(a)
    assert a.verdict in ("clean", "suspect")
    assert a.verdict != "lossy"


def test_padded_24_bit_is_reported_before_anything_else():
    a = _mk(declared_bits=24, effective_bits=16)
    spectral._verdict(a)
    assert a.verdict == "padded"


def test_upsampled_high_resolution_is_caught():
    a = _mk(sample_rate=96000, nyquist_hz=48000.0, cutoff_hz=20000.0, wall_db=40.0, above_db=-110.0)
    spectral._verdict(a)
    assert a.verdict == "upsampled"


# ---- file handling ----------------------------------------------------------------------

def test_find_audio_files_skips_the_quarantine_folder(tmp_path):
    (tmp_path / "a.flac").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    q = tmp_path / spectral.QUARANTINE_DIRNAME / "sub"
    q.mkdir(parents=True)
    (q / "b.flac").write_bytes(b"x")
    found = [p.name for p in spectral.find_audio_files(tmp_path)]
    assert found == ["a.flac"]


def test_quarantine_then_restore_round_trip(tmp_path):
    src = tmp_path / "Label" / "track.flac"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"audio")

    out = spectral.quarantine_files([str(src)], str(tmp_path))
    assert out["moved"] == 1
    assert not src.exists()

    back = spectral.restore_quarantined(out["quarantine_dir"])
    assert back["restored"] == 1
    assert src.read_bytes() == b"audio"


def test_quarantine_never_overwrites_a_same_named_track(tmp_path):
    for disc in ("CD1", "CD2"):
        p = tmp_path / disc / "01.flac"
        p.parent.mkdir(parents=True)
        p.write_bytes(disc.encode())
    spectral.quarantine_files([str(tmp_path / "CD1" / "01.flac")], str(tmp_path))
    spectral.quarantine_files([str(tmp_path / "CD2" / "01.flac")], str(tmp_path))
    held = sorted(p.read_bytes() for p in (tmp_path / spectral.QUARANTINE_DIRNAME).rglob("*.flac"))
    assert held == [b"CD1", b"CD2"]


@pytest.mark.parametrize("action", [spectral.quarantine_files, spectral.delete_files])
def test_actions_refuse_paths_outside_the_scanned_folder(tmp_path, action):
    outside = tmp_path / "elsewhere.flac"
    outside.write_bytes(b"x")
    root = tmp_path / "library"
    root.mkdir()
    result = action([str(outside)], str(root))
    assert outside.exists()
    assert result["failed"] and "outside" in result["failed"][0]["error"]


def test_report_lists_findings_and_leaves_out_the_clean_ones():
    scan = {
        "root": "/music", "scanned": 2, "total_files": 2,
        "counts": {"lossy": 1, "clean": 1},
        "results": [
            _mk(name="bad.flac", path="/music/bad.flac", verdict="lossy", confidence=95,
                estimated_source="320 kbps", reasons=["brick wall"]).to_dict(),
            _mk(name="good.flac", path="/music/good.flac", verdict="clean").to_dict(),
        ],
    }
    text = spectral.format_report(scan)
    assert "bad.flac" in text
    assert "320 kbps" in text
    assert "good.flac" not in text


# ---- the round-trip controls ------------------------------------------------------------

def _write_full_band_wav(path: Path, seconds: int = 8) -> None:
    """Pink-ish noise: real content all the way to Nyquist, so anything an encoder
    removes shows up as an absence rather than as material the source never had."""
    sr, n = 44100, 44100 * seconds
    rng = np.random.default_rng(7)
    x = rng.normal(0, 1, (n, 2))
    spec = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    freqs[0] = 1.0
    spec /= freqs[:, None] ** 0.5
    x = np.fft.irfft(spec, axis=0)
    x = x / np.max(np.abs(x)) * 0.25
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def _ffmpeg(*args: str) -> None:
    subprocess.run([spectral.ffmpeg_exe(), "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def controls(tmp_path_factory):
    d = tmp_path_factory.mktemp("controls")
    src = d / "source.wav"
    _write_full_band_wav(src)
    _ffmpeg("-i", str(src), "-c:a", "flac", str(d / "clean.flac"))
    _ffmpeg("-i", str(src), "-b:a", "320k", str(d / "c.mp3"))
    _ffmpeg("-i", str(d / "c.mp3"), "-c:a", "flac", "-sample_fmt", "s16", str(d / "mp3_320.flac"))
    _ffmpeg("-i", str(src), "-c:a", "aac", "-b:a", "256k", str(d / "c.m4a"))
    _ffmpeg("-i", str(d / "c.m4a"), "-c:a", "flac", "-sample_fmt", "s16", str(d / "aac_256.flac"))
    return d


@needs_ffmpeg
def test_a_genuine_lossless_file_is_cleared(controls):
    a = spectral.analyse(str(controls / "clean.flac"))
    assert a.verdict == "clean", a.reasons
    assert a.cutoff_hz > 21500


@needs_ffmpeg
@pytest.mark.parametrize("name,expected_cutoff", [("mp3_320.flac", 20200), ("aac_256.flac", 20100)])
def test_a_transcode_is_caught(controls, name, expected_cutoff):
    a = spectral.analyse(str(controls / name))
    assert a.verdict == "lossy", (a.verdict, a.cutoff_hz, a.wall_db, a.above_db)
    assert abs(a.cutoff_hz - expected_cutoff) < 700
    assert a.above_db < -90


@needs_ffmpeg
def test_aac_in_m4a_is_not_reported_as_a_fake(controls):
    """`.m4a` carries both ALAC and AAC, and bp-dl's own medium quality writes AAC.
    A file that is meant to be lossy is not a fake and must not be listed as one."""
    a = spectral.analyse(str(controls / "c.m4a"))
    assert a.verdict == "lossy_format"


@needs_ffmpeg
def test_scan_folder_sorts_the_worst_first(controls):
    scan = spectral.scan_folder(str(controls))
    verdicts = [r["verdict"] for r in scan["results"]]
    assert verdicts[0] == "lossy"
    assert verdicts.count("lossy") == 2
    assert "clean" in verdicts


def test_unreadable_file_is_a_result_not_a_crash(tmp_path):
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"not actually a flac file")
    a = spectral.analyse(str(broken))
    assert a.verdict == "unreadable"
    assert not a.ok


# ---- spectrogram ------------------------------------------------------------------------

def test_pow2_height_rounds_down_to_a_power_of_two():
    """The picture is drawn one frequency bin per pixel row, so a height that is not
    a power of two renders only the BOTTOM of the spectrum — at 520 px a 44.1 kHz file
    came out as 0–11 kHz, putting every brick wall above the top edge of the image."""
    assert spectral._pow2_height(520) == 512
    assert spectral._pow2_height(1024) == 1024
    assert spectral._pow2_height(1000) == 512
    assert spectral._pow2_height(10) == 128     # clamped up
    assert spectral._pow2_height(99999) == 2048  # clamped down


def _tone_wav(path: Path, freqs=(5000, 16000, 20000), seconds: int = 4) -> None:
    sr = 44100
    t = np.arange(sr * seconds) / sr
    x = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs) * 0.5
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.stack([x, x], 1) * 32767).astype("<i2").tobytes())


def _png_gray(path: Path) -> np.ndarray:
    """Read a PNG back as greyscale using ffmpeg, so the test needs no image library."""
    probe = subprocess.run(
        [spectral.ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, errors="replace",
    ).stderr
    dims = re.search(r"(\d{2,5})x(\d{2,5})", probe)
    w, h = int(dims.group(1)), int(dims.group(2))
    raw = subprocess.run(
        [spectral.ffmpeg_exe(), "-v", "error", "-i", str(path),
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(h, w)


@needs_ffmpeg
def test_spectrogram_shows_the_whole_band_up_to_nyquist(tmp_path):
    """Regression for the truncation trap: a 20 kHz tone must appear near the TOP of
    the picture. When the height was not a power of two it was simply not drawn."""
    src = tmp_path / "tones.wav"
    _tone_wav(src)
    # 520 is deliberately NOT a power of two — the height the first version shipped
    # with, and the one that silently truncated the picture at 11 kHz.
    png = spectral.spectrogram_png(str(src), str(tmp_path / "cache"), size=(1024, 520))
    img = _png_gray(png).astype(float)

    column = img[:, img.shape[1] // 2]
    lit = np.where(column > 60)[0]
    top, bottom = lit.min(), lit.max()  # the plot's own border lines
    inner = [i for i in lit if top + 3 < i < bottom - 3]
    assert inner, "no tones drawn at all"
    highest = min(inner)
    fraction = (bottom - highest) / (bottom - top)
    assert fraction > 0.85, f"highest tone at {fraction:.0%} of the plot — band is truncated"


@needs_ffmpeg
def test_spectrogram_is_cached_per_file(tmp_path):
    src = tmp_path / "tones.wav"
    _tone_wav(src, seconds=2)
    cache = tmp_path / "cache"
    first = spectral.spectrogram_png(str(src), str(cache))
    stamp = first.stat().st_mtime_ns
    again = spectral.spectrogram_png(str(src), str(cache))
    assert again == first and again.stat().st_mtime_ns == stamp


def test_read_tags_survives_a_file_with_none(tmp_path):
    p = tmp_path / "bare.flac"
    p.write_bytes(b"nonsense")
    assert spectral.read_tags(str(p)) == {"artist": "", "title": "", "album": "", "date": ""}


# ---- Windows / packaging ------------------------------------------------------------------

def test_safe_name_strips_characters_windows_rejects():
    """Track titles carry `?`, `:` and `/` routinely — a remix credit can hold two —
    and every one of them is illegal in a Windows filename."""
    out = spectral._safe_name('lossy - A:B / C? "D" <E> |F|')
    assert not set(out) & set('<>:"/\\|?*')
    assert out.startswith("lossy")
    assert spectral._safe_name("...") == "spectrogram"


def test_ffmpeg_env_override_wins(tmp_path, monkeypatch):
    fake = tmp_path / "my-ffmpeg"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("BPDL_FFMPEG", str(fake))
    assert spectral.ffmpeg_exe() == str(fake)


def test_ffmpeg_env_override_must_exist(monkeypatch, tmp_path):
    """A typo'd override has to say so, not silently fall through to a different
    ffmpeg than the one the user asked for."""
    monkeypatch.setenv("BPDL_FFMPEG", str(tmp_path / "nope"))
    with pytest.raises(spectral.FFmpegMissing):
        spectral.ffmpeg_exe()


def test_ffmpeg_beside_the_executable_is_preferred(tmp_path, monkeypatch):
    """How the packaged build finds its decoder: next to the running program, before
    anything on PATH."""
    monkeypatch.delenv("BPDL_FFMPEG", raising=False)
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    bundled = exe_dir / name
    bundled.write_text("#!/bin/sh\n")
    monkeypatch.setattr(spectral.sys, "frozen", True, raising=False)
    monkeypatch.setattr(spectral.sys, "executable", str(exe_dir / "bpdl-web"), raising=False)
    assert spectral.ffmpeg_exe() == str(bundled)


def test_under_is_case_insensitive_where_the_filesystem_is():
    """On Windows `C:\\Music` and `c:\\music` are one folder to the filesystem and two
    to pathlib — without this a delete refuses a file the user can plainly see."""
    assert spectral._under_ci(r"C:\Music", r"c:\music\x.flac")
    assert spectral._under_ci("C:\\Music\\", "C:/Music/CD1/x.flac")
    assert not spectral._under_ci(r"C:\Music", r"c:\other\x.flac")
    # A sibling folder that merely starts with the same letters is not inside it.
    assert not spectral._under_ci(r"C:\Music", r"c:\musicals\x.flac")


@needs_ffmpeg
def test_export_spectrograms_writes_readable_names(controls, tmp_path):
    results = [{"path": str(controls / "mp3_320.flac"), "name": "mp3_320.flac",
                "verdict": "lossy", "artist": "Some Artist", "album": "An Album",
                "title": "A Track"}]
    out = spectral.export_spectrograms(results, str(tmp_path / "cache"), str(tmp_path / "out"))
    assert out["saved"] == 1 and not out["failed"]
    saved = list((tmp_path / "out").glob("*.png"))
    assert len(saved) == 1
    assert saved[0].name == "lossy - Some Artist - An Album - A Track.png"
