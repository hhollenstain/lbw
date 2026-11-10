from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from ruamel.yaml import YAML

from agent_lbw import __version__
from agent_lbw.config import Settings
from agent_lbw.hardware import command_available, detect_hardware
from agent_lbw.llm.client import (
    OpenAICompatClient,
    check_model_endpoint,
    effective_context_window,
    normalize_model_base_url,
)
from agent_lbw.logging import setup_logging
from agent_lbw.runtime.improve import run_cycle
from agent_lbw.runtime.local_github import LOCAL_GITHUB_FILE, export_github, hydrate_github
from agent_lbw.runtime.project import write_default_project_files
from agent_lbw.runtime.watch import watch as watch_loop
from agent_lbw.tools.kiwi import check_kiwifs

REQUIRED_ENV_VARS: list[str] = ["LBW_MODEL_NAME", "LBW_MODEL_BASE_URL"]


app = typer.Typer(
    name="lbw",
    help="Look both ways: plan a change, build it, and hold the merge until the goal is met.",
    no_args_is_help=True,
)
console = Console()


def _load_config_file() -> None:
    """Load environment variables from a config file if it exists.

    Supports .env, config.yaml, and config.yml in the current directory.
    """
    yaml: YAML = YAML()
    config_paths: list[Path] = [
        Path.cwd() / ".env",
        Path.cwd() / "config.yaml",
        Path.cwd() / "config.yml",
    ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                if config_path.name == ".env":
                    with open(config_path) as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                key, _, value = line.partition("=")
                                os.environ[key.strip()] = value.strip()
                else:
                    with open(config_path) as f:
                        data: dict[str, Any] = yaml.load(f)
                        if isinstance(data, dict):
                            for key, value in data.items():
                                if isinstance(value, (str, int, float, bool)):
                                    os.environ.setdefault(f"LBW_{key.upper()}", str(value))
                console.print(f"[green]Loaded config from {config_path}[/green]")
                return
            except Exception:
                console.print(f"[yellow]Warning: Could not load config from {config_path}[/yellow]")
                return


def _validate_env(require: bool = True) -> None:
    """Check required environment variables and exit with a helpful message if missing.

    Args:
        require: If False, skip validation to allow UI mode without env vars.

    Returns:
        None (exits with SystemExit on missing variables)
    """
    if not require:
        return

    # Try to load config file first
    _load_config_file()

    missing: list[str] = [var for var in REQUIRED_ENV_VARS if var not in os.environ]
    if missing:
        console.print("[red]ERROR:[/red] Required environment variables not set:")
        for var in missing:
            console.print(f"  - {var}")
        console.print("\nPlease create a .env file from .env.example or export these variables.")
        console.print("\nYou may also create config.yaml or config.yml in the current directory.")
        raise SystemExit(1)


def _settings(**overrides: dict[str, Any]) -> Settings:
    """Create settings with optional overrides.

    Args:
        **overrides: Settings to override. Can include model_name, model_base_url,
            create_pr, auto_commit, and any other Settings field.

    Returns:
        A Settings instance with the overrides applied.
    """
    settings: Settings = Settings()
    for key, value in overrides.items():
        if value is not None:
            setattr(settings, key, value)
    settings.model_base_url = normalize_model_base_url(settings.model_base_url)
    setup_logging(settings.log_level)
    return settings


def _llm(settings: Settings) -> OpenAICompatClient:
    """Create an OpenAI-compatible client with the given settings.

    Args:
        settings: The configuration settings for the LLM client

    Returns:
        An OpenAICompatClient instance
    """
    return OpenAICompatClient(
        model=settings.model_name,
        base_url=settings.model_base_url,
        api_key=settings.model_api_key,
        context_window=settings.context_window,
    )


def _reject_context_window(context_window: int | None) -> None:
    if context_window is None:
        return
    if 512 <= context_window <= 1_048_576:
        return
    console.print("[red]context window must be between 512 and 1048576 tokens[/red]")
    raise typer.Exit(code=2)


def _cycle_context_window(settings: Settings) -> int | None:
    return effective_context_window(
        configured=settings.context_window,
        base_url=settings.model_base_url,
        api_key=settings.model_api_key,
        model=settings.model_name,
    )


def _print_version(value: bool) -> None:
    """Print the version and exit.

    Args:
        value: Whether to print the version

    Returns:
        None (exits with typer.Exit)
    """
    if value:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the LBW version and exit.",
            callback=_print_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Look both ways before a change merges.

    This function serves as the main entry point for the CLI application.
    It handles the --version option and performs no other operations.

    Args:
        version: Whether to print the version and exit

    Returns:
        None
    """
    pass


@app.command()
def doctor() -> None:
    """Report hardware, tooling, and model-endpoint health."""
    _load_config_file()
    _validate_env()
    settings: Settings = _settings()
    hw = detect_hardware()

    table: Table = Table(title="LBW")
    table.add_column("Check")
    table.add_column("Value")
    table.add_row("os", f"{hw.os_name}/{hw.arch}")
    table.add_row("apple silicon", "yes" if hw.is_apple_silicon else "no")
    table.add_row("nvidia", hw.gpu_name or "no")
    table.add_row("recommended backend", hw.recommended_backend)
    table.add_row("recommended model", hw.recommended_model)
    table.add_row("git", "yes" if command_available("git") else "missing")
    table.add_row("docker", "yes" if command_available("docker") else "missing (optional)")
    table.add_row("gh", "yes" if command_available("gh") else "missing (needed for PRs)")
    table.add_row("model url", settings.model_base_url)
    table.add_row("model name", settings.model_name)
    window = _cycle_context_window(settings)
    if settings.context_window:
        window_label = f"{window} (set)"
    elif window:
        window_label = f"{window} (server)"
    else:
        window_label = "unknown"
    table.add_row("context window", window_label)

    ok: bool
    detail: str
    ok, detail = check_model_endpoint(settings.model_base_url, settings.model_api_key)
    table.add_row("model endpoint", detail if ok else f"DOWN — {detail}")
    kiwi_ok: bool
    kiwi_detail: str
    kiwi_ok, kiwi_detail = check_kiwifs(settings.kiwifs_url, settings.kiwifs_api_key)
    if not settings.kiwifs_url.strip():
        table.add_row("kiwifs", "off")
    else:
        table.add_row("kiwifs", kiwi_detail if kiwi_ok else f"DOWN — {kiwi_detail}")
    console.print(table)
    for note in hw.notes:
        console.print(f"[yellow]{note}[/yellow]")
    if settings.kiwifs_url.strip() and not kiwi_ok:
        console.print(
            "[yellow]KiwiFS is the project's memory for local models. "
            "Start it with `kiwifs serve`, or set LBW_KIWIFS_URL empty "
            "to turn the tools off.[/yellow]"
        )
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def init(
    workspace: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    """Create .lbw/config.yaml and .lbw/goals.md in a project."""
    _validate_env()
    created: list[Path] | None = write_default_project_files(workspace)
    if created:
        for path in created:
            console.print(f"created {path}")
    else:
        console.print("project already has .lbw config")


@app.command()
def clone(
    url: Annotated[str, typer.Argument(help="Git URL or local repo to clone.")],
    dest: Annotated[
        Path | None,
        typer.Argument(help="Directory to create. Defaults to ./<repo-name>."),
    ] = None,
) -> None:
    """Clone a git repo into the workspace volume and write .lbw scaffolding."""
    _validate_env()
    from agent_lbw.runtime.workspaces import clone_workspace

    if dest is None:
        parent: Path = Path.cwd()
        name: str | None = None
    else:
        target = dest.expanduser()
        parent: Path = target.parent
        name: str | None = target.name
        if not parent.exists():
            console.print(f"parent does not exist: {parent}")
            raise typer.Exit(code=1)
    try:
        cloned: dict[str, Any] = clone_workspace(
            url, parent, name=name, git_config=_github_clone_config(url)
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"cloned {cloned['path']}")


def _github_clone_config(url: str) -> list[str] | None:
    from agent_lbw.runtime.store.github import GitHubAppError, current_token
    from agent_lbw.tools.git import github_auth_args, parse_github_remote

    if parse_github_remote(url) is None:
        return None
    try:
        token = current_token(Path.cwd(), _settings().database_url)
    except (GitHubAppError, OSError, ValueError):
        return None
    if not token:
        return None
    return github_auth_args(token)


def _serve_ui(
    workspace: Path,
    settings: Settings,
    *,
    host: str,
    port: int,
    max_concurrent: int,
    default_goal: str | None = None,
    default_create_pr: bool | None = None,
) -> None:
    from agent_lbw.web_ui import serve

    console.print(f"Web UI on http://{host}:{port}  (Ctrl+C to stop)")
    if max_concurrent > 1:
        console.print(
            "[yellow]Multiple concurrent tasks will contend for CPU, RAM, "
            "and the local model server.[/yellow]"
        )
    serve(
        workspace,
        settings,
        host=host,
        port=port,
        max_concurrent=max_concurrent,
        default_goal=default_goal,
        default_create_pr=default_create_pr,
    )


@app.command()
def roles(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    """List the roles that carry a change, and which ones are enabled."""
    from agent_lbw.runtime.roles import list_roles

    table = Table(title="Roles")
    table.add_column("Role")
    table.add_column("Job")
    table.add_column("Enabled")
    table.add_column("What it does")
    for role in list_roles(workspace):
        table.add_row(
            role.title,
            role.job,
            "yes" if role.enabled else "no",
            role.description or role.name,
        )
    console.print(table)


@app.command()
def run(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    goal: Annotated[str | None, typer.Option("--goal", "-g")] = None,
    model_name: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Model on the connected LLM server."),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option(
            "--base-url",
            help="OpenAI-compatible LLM server (host:port or /v1 URL).",
        ),
    ] = None,
    create_pr: Annotated[bool | None, typer.Option("--create-pr/--no-create-pr")] = None,
    auto_commit: Annotated[bool | None, typer.Option("--commit/--no-commit")] = None,
    context_window: Annotated[
        int | None,
        typer.Option(
            "--context-window",
            help="Tokens this model can hold on this machine.",
        ),
    ] = None,
    continuous: Annotated[
        bool,
        typer.Option(
            "--continuous",
            help="If no goal, pick a best-practice or UX improvement and merge the PR.",
        ),
    ] = False,
    web_ui: Annotated[
        bool,
        typer.Option("--web-ui", help="Start the web UI instead of running one cycle."),
    ] = False,
) -> None:
    """Run one change: plan, build, review, then commit when the gates pass."""
    _validate_env(require=not web_ui)
    _reject_context_window(context_window)
    settings: Settings = _settings(
        model_name=model_name,
        model_base_url=base_url,
        create_pr=create_pr,
        auto_commit=auto_commit,
        context_window=context_window,
    )
    if web_ui:
        _serve_ui(
            workspace,
            settings,
            host="127.0.0.1",
            port=8080,
            max_concurrent=1,
            default_goal=goal,
            default_create_pr=create_pr,
        )
        return
    result = run_cycle(
        workspace,
        settings,
        _llm(settings),
        goal,
        cli_create_pr=True if continuous and create_pr is None else create_pr,
        continuous=continuous,
        context_window=_cycle_context_window(settings),
        store_root=workspace,
    )
    console.print(
        f"[bold]{result.status}[/bold] stage={result.stage or 'done'} "
        f"committed={result.committed} "
        f"published={result.published} merged={result.merged} tests={result.tests_passed}"
    )
    if result.goal:
        console.print(f"goal: {result.goal.splitlines()[0]}")
    if result.summary:
        console.print(result.summary)
    if result.reason and result.status != "success":
        console.print(f"[yellow]{result.reason}[/yellow]")
    elif result.reason and result.reason.startswith("no changes needed"):
        console.print(f"[green]{result.reason}[/green]")
    if result.status in {"failed", "error"}:
        raise typer.Exit(code=2)


@app.command("watch")
def watch_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    goal: Annotated[str | None, typer.Option("--goal", "-g")] = None,
    interval: Annotated[
        int | None,
        typer.Option("--interval", help="Seconds between cycles."),
    ] = None,
    model_name: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Model on the connected LLM server."),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option(
            "--base-url",
            help="OpenAI-compatible LLM server (host:port or /v1 URL).",
        ),
    ] = None,
    create_pr: Annotated[bool | None, typer.Option("--create-pr/--no-create-pr")] = None,
    context_window: Annotated[
        int | None,
        typer.Option(
            "--context-window",
            help="Tokens this model can hold on this machine.",
        ),
    ] = None,
    continuous: Annotated[
        bool,
        typer.Option(
            "--continuous",
            help="Keep picking best-practice and UX improvements, open PRs, and merge them.",
        ),
    ] = False,
) -> None:
    """Keep improving a project on an interval."""
    _reject_context_window(context_window)
    settings: Settings = _settings(
        model_name=model_name,
        model_base_url=base_url,
        watch_interval_seconds=interval,
        create_pr=True if continuous and create_pr is None else create_pr,
        context_window=context_window,
    )
    try:
        watch_loop(
            workspace,
            settings,
            _llm(settings),
            goal,
            continuous=continuous,
            cli_create_pr=True if continuous and create_pr is None else create_pr,
        )
    except KeyboardInterrupt:
        console.print("stopped")


@app.command("ui")
def ui_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    goal: Annotated[str | None, typer.Option("--goal", "-g")] = None,
    model_name: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Model on the connected LLM server."),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option(
            "--base-url",
            help="OpenAI-compatible LLM server (host:port or /v1 URL).",
        ),
    ] = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8080,
    max_concurrent: Annotated[
        int,
        typer.Option(
            "--max-concurrent",
            min=1,
            help="How many cycles may run at once. More than 1 is hard on a local machine.",
        ),
    ] = 1,
    create_pr: Annotated[bool | None, typer.Option("--create-pr/--no-create-pr")] = None,
    auto_commit: Annotated[bool | None, typer.Option("--commit/--no-commit")] = None,
    context_window: Annotated[
        int | None,
        typer.Option(
            "--context-window",
            help="Tokens this model can hold on this machine.",
        ),
    ] = None,
) -> None:
    """Start a local web UI to queue and run tasks.

    The UI does not require environment variables set; it will load them from
    a .env, config.yaml, or config.yml file if present, but does not fail if
    they are missing. Users can configure settings via the UI.
    """
    _reject_context_window(context_window)
    settings: Settings = _settings(
        model_name=model_name,
        model_base_url=base_url,
        create_pr=create_pr,
        auto_commit=auto_commit,
        context_window=context_window,
    )
    try:
        _serve_ui(
            workspace,
            settings,
            host=host,
            port=port,
            max_concurrent=max_concurrent,
            default_goal=goal,
            default_create_pr=create_pr,
        )
    except KeyboardInterrupt:
        console.print("stopped")


def _local_store(workspace: Path) -> tuple[Path, str]:
    return workspace, Settings().database_url


@app.command("export-github")
def export_github_command(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            exists=True,
            file_okay=False,
            resolve_path=True,
            help="Store root with .lbw/master.key. The Docker stack uses ./workspaces.",
        ),
    ] = Path("."),
    file: Annotated[
        Path,
        typer.Option(
            "--file",
            "-f",
            help=f"Untracked file that keeps the GitHub App. Default: {LOCAL_GITHUB_FILE}.",
        ),
    ] = Path(LOCAL_GITHUB_FILE),
) -> None:
    """Copy the GitHub App into an untracked local file."""
    root, database_url = _local_store(workspace)
    try:
        saved = export_github(root, file, database_url=database_url)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"Saved the GitHub App to {saved}")


@app.command("hydrate")
def hydrate_command(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            exists=True,
            file_okay=False,
            resolve_path=True,
            help="Store root with .lbw/master.key. The Docker stack uses ./workspaces.",
        ),
    ] = Path("."),
    file: Annotated[
        Path,
        typer.Option(
            "--file",
            "-f",
            help=f"Untracked file that keeps the GitHub App. Default: {LOCAL_GITHUB_FILE}.",
        ),
    ] = Path(LOCAL_GITHUB_FILE),
) -> None:
    """Restore the GitHub App from the local file."""
    root, database_url = _local_store(workspace)
    try:
        view = hydrate_github(root, file, database_url=database_url)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    login = str(view.get("account_login") or "")
    suffix = f" on {login}" if login else ""
    console.print(f"Restored GitHub App {view.get('app_id', '')}{suffix}")
