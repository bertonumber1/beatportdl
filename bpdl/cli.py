from __future__ import annotations

import argparse
import signal
import sys

from rich.console import Console

from bpdl import config as config_module
from bpdl import paths
from bpdl.api import BeatportClient
from bpdl.auth import Auth
from bpdl.banner import print_banner
from bpdl.handlers import App
from bpdl.links import ARTIST_LINK, LABEL_LINK, parse_url
from bpdl.scanner import rank_map, scan_artist, scan_label
from bpdl.tui import run_main_app

console = Console()

VERSION = "2.0.0"


def make_clients(cfg: config_module.AppConfig) -> tuple[BeatportClient, BeatportClient]:
    """Builds the Beatport/Beatsource clients and logs in (or refreshes the
    cached session). Runs on a worker thread from the TUI — safe to block."""
    cache_path, _ = paths.find_cache_file()
    auth = Auth(cfg.username, cfg.password, cache_path)
    bp = BeatportClient("beatport", cfg.proxy, auth)
    bs = BeatportClient("beatsource", cfg.proxy, auth)
    if not auth.load_cache():
        auth.init(bp)
    return bp, bs


def print_scan_results(stats) -> None:
    print(f"\n========== Scan Results — {stats.total} tracks ==========")
    print("\n[ Genres ]  (use in filter_genres:)")
    for e in rank_map(stats.genres):
        print(f"  {e.name:<42} {e.count:4d} tracks")
    if stats.subgenres:
        print("\n[ Subgenres ]  (use in filter_subgenres:)")
        for e in rank_map(stats.subgenres):
            print(f"  {e.name:<42} {e.count:4d} tracks")
    if stats.bpm_max > 0:
        print(f"\n[ BPM Range ]  {stats.bpm_min} – {stats.bpm_max}")
    print("\n[ Top Artists ]  (use in filter_artists:)")
    for e in rank_map(stats.artists)[:30]:
        print(f"  {e.name:<42} {e.count:4d} tracks")
    print("\n================================================")


def _print_progress(msg: str) -> None:
    print(f"\r  {msg}", end="", flush=True)


def run_scan(bp: BeatportClient, bs: BeatportClient, urls: list[str]) -> None:
    for url in urls:
        try:
            link = parse_url(url)
        except Exception as e:
            console.print(f"[red][{url}][/red] parse url: {e}")
            continue
        client = bs if link.store == "beatsource" else bp
        if link.type == LABEL_LINK:
            label = client.get_label(link.id)
            print(f"Scanning label: {label.name} (this may take a while for large catalogues)")
            stats = scan_label(client, link, _print_progress)
            print()
        elif link.type == ARTIST_LINK:
            artist = client.get_artist(link.id)
            print(f"Scanning artist: {artist.name}")
            stats = scan_artist(client, link, _print_progress)
            print()
        else:
            print("--scan only works with label or artist URLs")
            continue
        print_scan_results(stats)


def main() -> None:
    parser = argparse.ArgumentParser(prog="bpdl")
    parser.add_argument("--scan", action="store_true", help="Scan a label/artist URL and list stats without downloading")
    parser.add_argument("inputs", nargs="*", help="URLs or .txt files of URLs — skips the TUI if given")
    args = parser.parse_args()

    print_banner(VERSION)

    config_path, config_exists = paths.find_config_file()
    if config_exists:
        try:
            cfg = config_module.parse(config_path)
        except config_module.ConfigError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
    else:
        cfg = config_module.AppConfig()

    urls: list[str] = []
    for arg in args.inputs:
        if arg.endswith(".txt"):
            with open(arg) as f:
                urls.extend(line.strip() for line in f if line.strip())
        else:
            urls.append(arg)

    if args.scan:
        if not config_exists:
            console.print("[red]No config file yet — run bpdl once interactively to set up your account first.[/red]")
            sys.exit(1)
        bp, bs = make_clients(cfg)
        run_scan(bp, bs, urls)
        return

    if not urls:
        # Interactive TUI session: first-run setup (if needed) -> queue URLs -> download.
        urls = run_main_app(cfg, config_path, make_clients, first_run=not config_exists)
        if not urls:
            return
    else:
        if not config_exists:
            console.print("[red]No config file yet — run bpdl once with no arguments to set up your account first.[/red]")
            sys.exit(1)

    bp, bs = make_clients(cfg)
    app = App(cfg, bp, bs)

    def handle_sigint(signum, frame):
        console.print("\n[yellow]Shutdown signal received. Waiting for download workers to finish[/yellow]")
        app.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    futures = [app.global_pool.submit(app.handle_url, url) for url in urls]
    for f in futures:
        f.result()

    app.stats.print_summary()
    app.shutdown()


if __name__ == "__main__":
    main()
