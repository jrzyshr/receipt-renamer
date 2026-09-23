"""Command line interface for receipt-renamer."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from . import __version__
from .config import (
    DEFAULT_MIN_CONFIDENCE,
    NEEDS_REVIEW_DIRNAME,
    Settings,
    env_name_format,
    env_provider,
    load_env,
)
from .images import DEFAULT_JPEG_QUALITY, DEFAULT_MAX_EDGE
from .ledger import Ledger, new_run_id, undo_run
from .naming import FORMAT_SPECS, NameFormat
from .processor import (
    FileResult,
    RunSummary,
    process_file,
    process_paths,
    scan_input,
)
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

_NAME_FORMAT_HELP = "Filename layout: " + " | ".join(
    f"{fmt.value} ({FORMAT_SPECS[fmt].example})" for fmt in NameFormat
) + " (env: RECEIPT_RENAMER_NAME_FORMAT)."


def _resolve_name_format(value: Optional[NameFormat]) -> NameFormat:
    """CLI flag wins over RECEIPT_RENAMER_NAME_FORMAT, which wins over the default."""
    if value is not None:
        return value
    try:
        return env_name_format()
    except ValueError as exc:
        error_console.print(f"[red]Invalid RECEIPT_RENAMER_NAME_FORMAT:[/red] {exc}")
        raise typer.Exit(code=2) from exc


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
    name_format: NameFormat,
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
        name_format=name_format,
    )


def _get_provider_or_exit(name: str, model: Optional[str]):
    """Build the provider, showing progress: this is where Entra ID auth can block."""
    try:
        with console.status(f"Connecting to {name}..."):
            return get_provider(name, model)
    except ProviderError as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc


def _provider_banner(provider_name: str, vision) -> str:
    """Describe the active provider, including Azure endpoint and auth mode."""
    banner = f"[bold]{provider_name}[/bold]/[bold]{vision.model}[/bold]"
    endpoint = getattr(vision, "endpoint", None) or getattr(vision, "base_url", None)
    if endpoint:
        banner += f" @ {endpoint}"
    auth = getattr(vision, "auth", None)
    if auth:
        banner += f" [dim]({auth})[/dim]"
    return banner


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


def _result_line(result: FileResult) -> str:
    """One compact line describing a finished file, shared by run and watch."""
    original, target, outcome, detail = _result_row(result)
    style = _OUTCOME_STYLE.get(result.outcome, "white")
    arrow = f" → {target}" if target != "-" else ""
    suffix = f"  [dim]{detail}[/dim]" if detail else ""
    return f"[{style}]{outcome:>12}[/{style}]  {original}{arrow}{suffix}"


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
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Print a line for every file as it is processed, not just problem files.",
    ),
    in_place: bool = typer.Option(
        False, "--in-place", help="Rename inside the input folder instead of moving to an output folder."
    ),
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        "-p",
        help="openai | azure | anthropic | fake (env: RECEIPT_RENAMER_PROVIDER).",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Override the model. For --provider azure this is the DEPLOYMENT name.",
    ),
    min_confidence: float = typer.Option(
        DEFAULT_MIN_CONFIDENCE, "--min-confidence", help="Below this, files go to Needs Review."
    ),
    max_edge: int = typer.Option(
        DEFAULT_MAX_EDGE, "--max-edge", help="Downscale so the long edge is at most this many pixels."
    ),
    name_format: Optional[NameFormat] = typer.Option(
        None, "--name-format", help=_NAME_FORMAT_HELP
    ),    quality: int = typer.Option(DEFAULT_JPEG_QUALITY, "--quality", help="JPEG quality for uploads."),
) -> None:
    """Process a folder of receipts once. Dry run unless you pass --apply."""
    is_dry_run = not apply or dry_run
    resolved_name_format = _resolve_name_format(name_format)
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
        name_format=resolved_name_format,
    )
    vision = _get_provider_or_exit(provider_name, model)
    ledger = None if is_dry_run else Ledger(settings.ledger_path)

    console.print(
        f"Connected to {_provider_banner(provider_name, vision)}"
        + f" [dim](names: {settings.name_format.value})[/dim]"
        + (" [cyan](dry run)[/cyan]" if is_dry_run else "")
    )

    with console.status(f"Finding receipts in {settings.input_dir}..."):
        candidates, unsupported = scan_input(settings)
    console.print(
        f"Found [bold]{len(candidates)}[/bold] receipt(s) in [bold]{settings.input_dir}[/bold]"
        + (f"  [dim]({len(unsupported)} unreadable file(s) ignored)[/dim]" if unsupported else "")
    )
    if not candidates:
        console.print("[yellow]No supported image files found.[/yellow]")
        return

    summary = _process_with_progress(
        candidates, vision, settings, ledger=ledger, verbose=verbose
    )
    _render_table(summary.results, dry_run=is_dry_run)
    _render_summary(summary, settings)
    if summary.aborted:
        error_console.print(f"[red]Aborted:[/red] {summary.aborted_reason}")
        raise typer.Exit(code=1)


def _process_with_progress(
    candidates: list[Path],
    vision,
    settings: Settings,
    *,
    ledger: Optional[Ledger],
    verbose: bool,
) -> RunSummary:
    """Run the batch behind a live progress bar, streaming problems as they occur.

    Results are collected through the callback rather than only from the return value so
    a Ctrl-C still yields a usable (partial) summary -- important when files have already
    been moved and the user needs the run id to undo them.
    """
    run_id = new_run_id()
    collected: list[FileResult] = []
    interrupted = False

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Reading receipts", total=len(candidates))

        def on_file_start(path: Path) -> None:
            progress.update(task, description=f"Reading [bold]{path.name}[/bold]")

        def on_result(result: FileResult) -> None:
            collected.append(result)
            progress.advance(task)
            # Renamed files are already in the final table; only surface what needs
            # attention, unless the user asked to see everything.
            if verbose or result.outcome != "renamed":
                progress.console.print(_result_line(result))

        try:
            summary = process_paths(
                candidates,
                vision,
                settings,
                ledger=ledger,
                run_id=run_id,
                on_file_start=on_file_start,
                on_result=on_result,
            )
        except KeyboardInterrupt:
            interrupted = True
            summary = RunSummary(
                run_id=run_id,
                results=collected,
                dry_run=settings.dry_run,
                aborted_reason="interrupted by Ctrl-C",
            )
        progress.update(task, description="Done")

    if interrupted:
        console.print(
            f"\n[yellow]Interrupted[/yellow] after {len(collected)} of {len(candidates)} file(s)."
        )
    return summary


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
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="openai | azure | anthropic | fake."
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Override the model. For --provider azure this is the DEPLOYMENT name.",
    ),
    min_confidence: float = typer.Option(
        DEFAULT_MIN_CONFIDENCE, "--min-confidence", help="Below this, files go to Needs Review."
    ),
    debounce: float = typer.Option(
        DEFAULT_DEBOUNCE, "--debounce", help="Seconds a file must stop changing before it is read."
    ),
    max_edge: int = typer.Option(DEFAULT_MAX_EDGE, "--max-edge", help="Downscale long edge to this."),
    quality: int = typer.Option(DEFAULT_JPEG_QUALITY, "--quality", help="JPEG quality for uploads."),
    name_format: Optional[NameFormat] = typer.Option(
        None, "--name-format", help=_NAME_FORMAT_HELP
    ),
) -> None:
    """Watch a folder and rename scans as they land. Ctrl-C to stop."""
    resolved_name_format = _resolve_name_format(name_format)
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
        name_format=resolved_name_format,
    )
    vision = _get_provider_or_exit(provider_name, model)
    ledger = None if dry_run else Ledger(settings.ledger_path)
    run_id = new_run_id()
    reserved: set[Path] = set()

    def handle(path: Path) -> None:
        result = process_file(
            path, vision, settings, ledger=ledger, run_id=run_id, reserved=reserved
        )
        console.print(_result_line(result))

    console.print(
        f"Watching [bold]{settings.input_dir}[/bold] with "
        + _provider_banner(provider_name, vision)
        + f" [dim]debounce {debounce}s[/dim]"
        + f" [dim](names: {settings.name_format.value})[/dim]"
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
