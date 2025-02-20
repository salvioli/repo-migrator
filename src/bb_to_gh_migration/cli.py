import typer
from rich import print
from typing import Optional, List
import subprocess
import os
from dotenv import load_dotenv
from .migration import MigrationConfig, Migrator

app = typer.Typer(help="Bitbucket to GitHub migration tool")

# Load environment variables from .env file
load_dotenv()

def load_env_value(value: Optional[str]) -> Optional[str]:
    """Load environment value, evaluating shell commands if needed"""
    if not value:
        return None
    if value.startswith('$(') and value.endswith(')'):
        try:
            cmd = value[2:-1]  # Remove $( and )
            return subprocess.check_output(cmd, shell=True, text=True).strip()
        except subprocess.SubprocessError as e:
            logger.warning(f"Failed to execute shell command {cmd}: {e}")
            return None
    return value

def get_config(
    bb_username: str,
    bb_password: str,
    github_token: str,
    bb_workspace: str,
    gh_org: str,
    dry_run: bool,
    verbose: bool,
) -> MigrationConfig:
    """Create configuration from CLI options or environment variables."""
    return MigrationConfig(
        bb_username=bb_username or load_env_value(os.getenv('BB_USERNAME')),
        bb_password=bb_password or load_env_value(os.getenv('BB_PASSWORD')),
        github_token=github_token or load_env_value(os.getenv('GITHUB_TOKEN')),
        bb_workspace=bb_workspace or load_env_value(os.getenv('BB_WORKSPACE')),
        gh_org=gh_org or load_env_value(os.getenv('GH_ORG')),
        dry_run=dry_run,
        verbose=verbose,
    )

@app.command()
def test_connection(
    bb_username: str = typer.Option(None, help="Bitbucket username"),
    bb_password: str = typer.Option(None, help="Bitbucket password", prompt_required=False),
    github_token: str = typer.Option(None, help="GitHub token", prompt_required=False),
    bb_workspace: str = typer.Option(None, help="Bitbucket workspace"),
    gh_org: str = typer.Option(None, help="GitHub organization"),
    dry_run: bool = typer.Option(False, help="Perform dry run without making changes"),
    verbose: bool = typer.Option(False, help="Enable verbose logging"),
):
    """Test connections to Bitbucket and GitHub."""
    config = get_config(bb_username, bb_password, github_token, bb_workspace, gh_org, dry_run, verbose)
    tester = Migrator(config)
    success = tester.test_connections()

    if success:
        print("[green]Connection test successful! ✓[/green]")
        if config.verbose:
            print(f"Connected to Bitbucket workspace: [bold]{config.bb_workspace}[/bold]")
            print(f"Connected to GitHub organization: [bold]{config.gh_org}[/bold]")
    else:
        print("[red]Connection test failed! ✗[/red]")
        print("Failed to connect to Bitbucket or GitHub. Please check your credentials and permissions.")
        raise typer.Exit(1)
    
    tester.test_repository_listing()

@app.command()
def migrate_repo(
    repo_slugs: List[str] = typer.Argument(..., help="Repositories to migrate"),
    bb_username: str = typer.Option(None, help="Bitbucket username"),
    bb_password: str = typer.Option(None, help="Bitbucket password", prompt_required=False),
    github_token: str = typer.Option(None, help="GitHub token", prompt_required=False),
    bb_workspace: str = typer.Option(None, help="Bitbucket workspace"),
    gh_org: str = typer.Option(None, help="GitHub organization"),
    dry_run: bool = typer.Option(False, help="Simulate migration without making changes"),
    verbose: bool = typer.Option(False, help="Enable verbose logging"),
):
    """Migrate one or more repositories."""
    config = get_config(bb_username, bb_password, github_token, bb_workspace, gh_org, dry_run, verbose)
    migrator = Migrator(config)
    if not migrator.test_connections():
        raise typer.Exit(1)
    
    print(f"\n[bold]Starting migration of {len(repo_slugs)} repositories[/bold]")
    for i, repo_slug in enumerate(repo_slugs, 1):
        print(f"\n[bold]Migrating repository {i}/{len(repo_slugs)}: {repo_slug}[/bold]")
        migrator.migrate_single_repository(repo_slug)

@app.command()
def migrate_workspace(
    bb_username: str = typer.Option(None, help="Bitbucket username"),
    bb_password: str = typer.Option(None, help="Bitbucket password", prompt_required=False),
    github_token: str = typer.Option(None, help="GitHub token", prompt_required=False),
    bb_workspace: str = typer.Option(None, help="Bitbucket workspace"),
    gh_org: str = typer.Option(None, help="GitHub organization"),
    dry_run: bool = typer.Option(False, help="Simulate migration without making changes"),
    verbose: bool = typer.Option(False, help="Enable verbose logging"),
):
    """Migrate all repositories in the Bitbucket workspace."""
    config = get_config(bb_username, bb_password, github_token, bb_workspace, gh_org, dry_run, verbose)
    migrator = Migrator(config)
    if not migrator.test_connections():
        raise typer.Exit(1)
    migrator.migrate_workspace()

def main():
    app()

if __name__ == "__main__":
    main()
