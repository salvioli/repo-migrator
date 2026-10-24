"""Migration settings and secret resolution."""

import os
import shlex
import subprocess
from dataclasses import dataclass


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class MigrationConfig:
    bb_email: str
    bb_api_token: str
    bb_workspace: str
    github_token: str
    gh_org: str
    bb_api_url: str = "https://api.bitbucket.org/2.0"
    bb_git_url: str = "https://bitbucket.org"
    gh_api_url: str = "https://api.github.com"
    gh_git_url: str = "https://github.com"


def load_config(bb_email: str | None, bb_workspace: str | None, gh_org: str | None) -> MigrationConfig:
    """Build the configuration, failing with every missing setting at once."""
    values = {
        "BB_EMAIL": bb_email or os.environ.get("BB_EMAIL"),
        "BB_API_TOKEN": read_secret("BB_API_TOKEN"),
        "BB_WORKSPACE": bb_workspace or os.environ.get("BB_WORKSPACE"),
        "GITHUB_TOKEN": read_secret("GITHUB_TOKEN"),
        "GH_ORG": gh_org or os.environ.get("GH_ORG"),
    }
    missing: list[str] = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(f"Missing settings: {', '.join(missing)}")
    endpoints = {
        field: os.environ[env]
        for field, env in (
            ("bb_api_url", "BB_API_URL"),
            ("bb_git_url", "BB_GIT_URL"),
            ("gh_api_url", "GH_API_URL"),
            ("gh_git_url", "GH_GIT_URL"),
        )
        if os.environ.get(env)
    }
    return MigrationConfig(
        bb_email=values["BB_EMAIL"],
        bb_api_token=values["BB_API_TOKEN"],
        bb_workspace=values["BB_WORKSPACE"],
        github_token=values["GITHUB_TOKEN"],
        gh_org=values["GH_ORG"],
        **endpoints,
    )


def read_secret(name: str) -> str | None:
    """Read a secret from NAME, or from the output of the command in NAME_CMD.

    The command runs without a shell, so its value is never interpreted beyond
    shell-style word splitting.
    """
    value = os.environ.get(name)
    if value:
        return value
    command = os.environ.get(f"{name}_CMD")
    if not command:
        return None
    try:
        result = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=120, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        raise ConfigError(f"{name}_CMD could not be run: {e}") from None
    if result.returncode != 0:
        # stdout may hold part of the secret, so only stderr is reported.
        raise ConfigError(f"{name}_CMD exited with {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip() or None
