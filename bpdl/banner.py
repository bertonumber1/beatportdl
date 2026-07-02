import pyfiglet
from rich.console import Console
from rich.text import Text

console = Console()


def print_banner(version: str = "") -> None:
    console.print()
    console.print(Text("Smash-n-Grab's", style="bold white"))
    fig = pyfiglet.Figlet(font="big")
    console.print(Text(fig.renderText("BP-DL"), style="bold cyan"))
    console.print(Text("Beatport / Beatsource downloader", style="dim"))
    if version:
        console.print(Text(f"v{version}", style="dim"), justify="center")
    console.print()
