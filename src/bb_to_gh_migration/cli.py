import logging
from typing import Annotated

import typer
from dotenv import find_dotenv, load_dotenv
from rich import print

from .config import ConfigError, load_config
from .migration import Migrator, RepoResult

app = typer.Typer(help="Bitbucket to GitHub migration tool")

DryRun = Annotated[bool, typer.Option(help="Report what would be migrated without changing anything")]
Resume = Annotated[bool, typer.Option(help="Continue migrating into repositories that already exist on GitHub")]


@app.callback()
def main_options(
    ctx: typer.Context,
    bb_email: Annotated[str | None, typer.Option(envvar="BB_EMAIL", help="Atlassian account e-mail")] = None,
    bb_workspace: Annotated[str | None, typer.Option(envvar="BB_WORKSPACE", help="Bitbucket workspace")] = None,
    gh_org: Annotated[str | None, typer.Option(envvar="GH_ORG", help="GitHub organization or user")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging")] = False,
):
    """Secrets are read from BB_API_TOKEN and GITHUB_TOKEN, or from the output of the
    commands in BB_API_TOKEN_CMD and GITHUB_TOKEN_CMD."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(message)s")
    if not verbose:
        logging.getLogger("github").setLevel(logging.WARNING)
    ctx.obj = (bb_email, bb_workspace, gh_org)


def _connect(ctx: typer.Context) -> Migrator:
    try:
        config = load_config(*ctx.obj)
    except ConfigError as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None
    migrator = Migrator(config)
    if not migrator.check_connections():
        print("[red]Connection test failed. Check your credentials and permissions.[/red]")
        raise typer.Exit(1)
    return migrator


@app.command()
def test_connection(ctx: typer.Context):
    """Test connections to Bitbucket and GitHub."""
    slugs = _connect(ctx).workspace_repositories()
    print(f"[green]Connection test successful.[/green] {len(slugs)} repositories in the Bitbucket workspace.")
    for slug in slugs:
        print(f"  {slug}")


@app.command()
def migrate_repo(
    ctx: typer.Context,
    repo_slugs: Annotated[list[str], typer.Argument(help="Repositories to migrate")],
    dry_run: DryRun = False,
    resume: Resume = False,
):
    """Migrate one or more repositories."""
    _report(_connect(ctx).migrate(repo_slugs, dry_run=dry_run, resume=resume))


@app.command()
def migrate_workspace(ctx: typer.Context, dry_run: DryRun = False, resume: Resume = False):
    """Migrate all repositories in the Bitbucket workspace."""
    migrator = _connect(ctx)
    _report(migrator.migrate(migrator.workspace_repositories(), dry_run=dry_run, resume=resume))


def _report(results: list[RepoResult]) -> None:
    print("\n[bold]Summary[/bold]")
    for result in results:
        color = "green" if result.ok else "yellow" if result.status == "skipped" else "red"
        print(f"  [{color}]{result.status:8}[/{color}] {result.slug}: {result.detail}")
    if not all(result.ok for result in results):
        raise typer.Exit(1)


def main():
    load_dotenv(find_dotenv(usecwd=True))
    app()


if __name__ == "__main__":
    main()
