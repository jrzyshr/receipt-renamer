"""Command line interface for receipt-renamer."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import (
    DEFAULT_MIN_CONFIDENCE,
    NEEDS_REVIEW_DIRNAME,
    Settings,
    env_provider,
    load_env,
)
from .images import DEFAULT_JPEG_QUALITY, DEFAULT_MAX_EDGE
from .ledger import Ledger, new_run_id, undo_run
from .processor import FileResult, RunSummary, process_directory, process_file
from .providers import ProviderError, get_provider, resolve_provider_name
from .watcher import DEFAULT_DEBOUNCE, FolderWatcher

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Rename batch-scanned receipts to 'YYYY-MM-DD Business - Purpose.jpg' using a vision LLM.",
)
console = Console()
error_console = Console(stderr=True)

_OUTCOME_STYLE = {"renamed": "green", "needs_review": "yellow", "skipped": "dim"}


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"receipt-renamer {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Load .env early so every command sees the API keys."""
    load_env()


def _build_settings(
    input_dir: Path,
    output_dir: Optional[Path],
    *,
    in_place: bool,
    recursive: bool,
    dry_run: bool,
    min_confidence: float,
    max_edge: int,
    quality: int,
    provider_name: str,
    model: Optional[str],
) -> Settings:
    if not input_dir.exists() or not input_dir.is_dir():
        error_console.print(f"[red]Input folder does not exist:[/red] {input_dir}")
        raise typer.Exit(code=2)
    if in_place and output_dir is not None:
        error_console.print("[red]--in-place and --output are mutually exclusive.[/red]")
        raise typer.Exit(code=2)
    if not 0.0 <= min_confidence <= 1.0:
        error_console.print("[red]--min-confidence must be between 0.0 and 1.0.[/red]")
        raise typer.Exit(code=2)
    return Settings.create(
        input_dir,
        output_dir=output_dir,
        in_place=in_place,
        recursive=recursive,
        dry_run=dry_run,
        min_confidence=min_confidence,
        max_edge=max_edge,
        jpeg_quality=quality,
        provider_name=provider_name,
        model=model,
    )


def _get_provider_or_exit(name: str, model: Optional[str]):
    try:
        return get_provider(name, model)
    except ProviderError as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc


def _result_row(result: FileResult) -> tuple[str, str, str, str]:
    if result.outcome == "renamed":
        assert result.destination is not None
        target = result.destination.name
        detail = f"{result.data.confidence:.2f}" if result.data else ""
    elif result.outcome == "needs_review":
        target = f"{NEEDS_REVIEW_DIRNAME}/{result.destination.name if result.destination else ''}"
        detail = result.reason or ""
    else:
        target = "-"
        detail = result.reason or ""
    return result.original.name, target, result.outcome.replace("_", " "), detail


def _render_table(results: list[FileResult], *, dry_run: bool) -> None:
    if not results:
        console.print("[yellow]No supported image files found.[/yellow]")
        return
    table = Table(
        title="Planned renames (dry run)" if dry_run else "Renames applied",
        header_style="bold",
        show_lines=False,
    )
    table.add_column("Original", overflow="fold")
    table.add_column("→ New name", overflow="fold")
    table.add_column("Outcome")
    table.add_column("Confidence / reason", overflow="fold")
    for result in results:
        original, target, outcome, detail = _result_row(result)
        style = _OUTCOME_STYLE.get(result.outcome, "")
        table.add_row(original, target, f"[{style}]{outcome}[/{style}]" if style else outcome, detail)
    console.print(table)


def _render_summary(summary: RunSummary, settings: Settings) -> None:
    console.print(
        f"[bold]{summary.renamed}[/bold] renamed  "
        f"[yellow]{summary.needs_review}[/yellow] needs review  "
        f"[dim]{summary.skipped}[/dim] skipped"
    )
    if summary.dry_run:
        console.print(
            "[cyan]Dry run - nothing was moved. Re-run with [bold]--apply[/bold] to make it real.[/cyan]"
        )
    else:
        console.print(f"Output folder: [bold]{settings.output_dir}[/bold]")
        console.print(f"Ledger: {settings.ledger_path}  (run id: {summary.run_id})")
        console.print("Undo this run with: [bold]receipt-renamer undo --input "
                      f"{settings.input_dir}[/bold]")


@app.command()
def run(
    input_dir: Path = typer.Option(..., "--input", "-i", help="Folder containing scanned receipts."),
    output_dir: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Where renamed files go (default: <input>/Renamed)."
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Actually move/rename files. Without this, nothing is touched."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Explicitly request a dry run (the default behaviour)."
    ),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Recurse into subfolders."),
    in_place: bool = typer.Option(
        False, "--in-place", help="Rename inside the input folder instead of moving to an output folder."
    ),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="openai | anthropic | fake (env: RECEIPT_RENAMER_PROVIDER)."
    ),
    model: Optional[str] = typer.Option(None, "--model", help="Override the provider's default model."),
    min_confidence: float = typer.Option(
        DEFAULT_MIN_CONFIDENCE, "--min-confidence", help="Below this, files go to Needs Review."
    ),
    max_edge: int = typer.Option(
        DEFAULT_MAX_EDGE, "--max-edge", help="Downscale so the long edge is at most this many pixels."
    ),
    quality: int = typer.Option(DEFAULT_JPEG_QUALITY, "--quality", help="JPEG quality for uploads."),
) -> None:
    """Process a folder of receipts once. Dry run unless you pass --apply."""
    is_dry_run = not apply or dry_run
    provider_name = resolve_provider_name(provider or env_provider())
    settings = _build_settings(
        input_dir.expanduser(),
        output_dir.expanduser() if output_dir else None,
        in_place=in_place,
        recursive=recursive,
        dry_run=is_dry_run,
        min_confidence=min_confidence,
        max_edge=max_edge,
        quality=quality,
        provider_name=provider_name,
        model=model,
    )
    vision = _get_provider_or_exit(provider_name, model)
    ledger = None if is_dry_run else Ledger(settings.ledger_path)

    console.print(
        f"Scanning [bold]{settings.input_dir}[/bold] with "
        f"[bold]{provider_name}[/bold]/[bold]{vision.model}[/bold]"
        + (" [cyan](dry run)[/cyan]" if is_dry_run else "")
    )
    with console.status("Reading receipts..."):
        summary = process_directory(vision, settings, ledger=ledger)
    _render_table(summary.results, dry_run=is_dry_run)
    _render_summary(summary, settings)


@app.command()
def watch(
    input_dir: Path = typer.Option(..., "--input", "-i", help="Folder to watch for new scans."),
    output_dir: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Where renamed files go (default: <input>/Renamed)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would happen without moving anything."
    ),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Watch subfolders too."),
    in_place: bool = typer.Option(False, "--in-place", help="Rename inside the watched folder."),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="openai | anthropic | fake."),
    model: Optional[str] = typer.Option(None, "--model", help="Override the provider's default model."),
    min_confidence: float = typer.Option(
        DEFAULT_MIN_CONFIDENCE, "--min-confidence", help="Below this, files go to Needs Review."
    ),
    debounce: float = typer.Option(
        DEFAULT_DEBOUNCE, "--debounce", help="Seconds a file must stop changing before it is read."
    ),
    max_edge: int = typer.Option(DEFAULT_MAX_EDGE, "--max-edge", help="Downscale long edge to this."),
    quality: int = typer.Option(DEFAULT_JPEG_QUALITY, "--quality", help="JPEG quality for uploads."),
) -> None:
    """Watch a folder and rename scans as they land. Ctrl-C to stop."""
    provider_name = resolve_provider_name(provider or env_provider())
    settings = _build_settings(
        input_dir.expanduser(),
        output_dir.expanduser() if output_dir else None,
        in_place=in_place,
        recursive=recursive,
        dry_run=dry_run,
        min_confidence=min_confidence,
        max_edge=max_edge,
        quality=quality,
        provider_name=provider_name,
        model=model,
    )
    vision = _get_provider_or_exit(provider_name, model)
    ledger = None if dry_run else Ledger(settings.ledger_path)
    run_id = new_run_id()
    reserved: set[Path] = set()

    def handle(path: Path) -> None:
        result = process_file(
            path, vision, settings, ledger=ledger, run_id=run_id, reserved=reserved
        )
        original, target, outcome, detail = _result_row(result)
        style = _OUTCOME_STYLE.get(result.outcome, "white")
        console.print(f"[{style}]{outcome:>12}[/{style}]  {original} → {target}  {detail}")

    console.print(
        f"Watching [bold]{settings.input_dir}[/bold] "
        f"({provider_name}/{vision.model}, debounce {debounce}s)"
        + (" [cyan](dry run)[/cyan]" if dry_run else "")
    )
    console.print("[dim]Press Ctrl-C to stop.[/dim]")
    FolderWatcher(settings, handle, debounce=debounce).run_forever()
    console.print("\nStopped watching.")


@app.command()
def undo(
    input_dir: Path = typer.Option(
        ..., "--input", "-i", help="Folder whose ledger should be reversed."
    ),
    run_id: Optional[str] = typer.Option(
        None, "--run-id", help="Undo a specific run id instead of the most recent one."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what undo would restore."),
    ledger_path: Optional[Path] = typer.Option(
        None, "--ledger", help="Path to the ledger file (default: <input>/.receipt-renamer-log.jsonl)."
    ),
) -> None:
    """Reverse the most recent run using the ledger."""
    settings = Settings.create(input_dir.expanduser(), ledger_path=ledger_path)
    ledger = Ledger(settings.ledger_path)
    if not ledger.path.exists():
        error_console.print(f"[red]No ledger found at[/red] {ledger.path}")
        raise typer.Exit(code=1)

    target, results = undo_run(ledger, run_id, dry_run=dry_run)
    if target is None:
        console.print("[yellow]Nothing to undo.[/yellow]")
        return
    if not results:
        console.print(f"[yellow]Run {target} moved no files.[/yellow]")
        return

    table = Table(title=f"Undo {target}" + (" (dry run)" if dry_run else ""), header_style="bold")
    table.add_column("Current", overflow="fold")
    table.add_column("→ Restored to", overflow="fold")
    table.add_column("Status")
    styles = {"restored": "green", "missing": "yellow", "blocked": "red"}
    for item in results:
        table.add_row(
            item.new_path.name,
            item.original_path.name,
            f"[{styles[item.status]}]{item.status}[/{styles[item.status]}]"
            + (f" ({item.reason})" if item.reason else ""),
        )
    console.print(table)
    restored = sum(1 for item in results if item.status == "restored")
    console.print(
        f"[bold]{restored}[/bold] of {len(results)} file(s) "
        + ("would be restored." if dry_run else "restored.")
    )


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
