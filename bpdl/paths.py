from __future__ import annotations

import os
import sys
from pathlib import Path

CONFIG_FILENAME = "bpdl-config.yml"
CACHE_FILENAME = "bpdl-credentials.json"
ERROR_LOG_FILENAME = "bpdl-err.log"


def _executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(sys.argv[0]).resolve().parent


def _candidate_paths(filename: str, extra_dirs: list[Path]) -> list[Path]:
    candidates = [Path.cwd() / filename, _executable_dir() / filename]
    candidates += [d / filename for d in extra_dirs]
    return candidates


def _find(filename: str, extra_dirs: list[Path]) -> tuple[Path, bool]:
    candidates = _candidate_paths(filename, extra_dirs)
    for candidate in candidates:
        if candidate.exists():
            return candidate, True
    # Nothing exists yet — default to the working directory (in the Docker image
    # this is /config, the bind-mounted volume) rather than an OS-convention XDG
    # path buried under it, so a fresh config lands somewhere obvious.
    return candidates[0], False


def find_config_file() -> tuple[Path, bool]:
    extra_dirs = []
    if sys.platform.startswith("linux"):
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
        extra_dirs.append(base / "bpdl")
    return _find(CONFIG_FILENAME, extra_dirs)


def find_cache_file() -> tuple[Path, bool]:
    extra_dirs = []
    if sys.platform.startswith("linux"):
        xdg = os.environ.get("XDG_STATE_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "state"
        extra_dirs.append(base / "bpdl")
    return _find(CACHE_FILENAME, extra_dirs)


def find_error_log_file() -> tuple[Path, bool]:
    return _find(ERROR_LOG_FILENAME, [])
