"""Mirror git repositories, including Git LFS objects, between two HTTPS remotes.

Credentials never appear in URLs or command lines. They are handed to git
through environment variables read by an inline credential helper scoped to
each remote's base URL, so they stay out of logs, error messages and `ps`.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Remote:
    base_url: str
    path: str
    username: str
    password: str

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.path}.git"


def mirror(source: Remote, target: Remote) -> None:
    """Copy every ref and LFS object from source to target, overwriting target refs."""
    env = _git_env([source, target])
    with tempfile.TemporaryDirectory(prefix="bb2gh-") as temp_dir:
        repo_dir = str(Path(temp_dir) / "repo.git")
        logger.info("Cloning %s", source.url)
        _run(["git", "clone", "--mirror", "--quiet", source.url, repo_dir], env)
        if _uses_lfs(repo_dir, env):
            if shutil.which("git-lfs") is None:
                raise GitError("Repository uses Git LFS but git-lfs is not installed")
            logger.info("Copying Git LFS objects")
            _run(["git", "lfs", "fetch", "--all", "origin"], env, cwd=repo_dir)
            _run(["git", "lfs", "push", "--all", target.url], env, cwd=repo_dir)
        logger.info("Pushing to %s", target.url)
        _run(["git", "push", "--mirror", "--quiet", target.url], env, cwd=repo_dir)


def _git_env(remotes: list[Remote]) -> dict[str, str]:
    # Drop only variables that would clash with the config injected below.
    env = {
        k: v for k, v in os.environ.items()
        if not re.fullmatch(r"GIT_CONFIG_(COUNT|KEY_\d+|VALUE_\d+|PARAMETERS)|GIT_ASKPASS|SSH_ASKPASS", k)
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    # An empty helper resets the list, so the user's own helpers (for example
    # osxkeychain) can neither answer for nor store these credentials.
    config = [("credential.helper", "")]
    for index, remote in enumerate(remotes):
        user_var, password_var = f"BB2GH_GIT_USERNAME_{index}", f"BB2GH_GIT_PASSWORD_{index}"
        env[user_var], env[password_var] = remote.username, remote.password
        # Answers only "get", so git never asks it to store or erase anything.
        helper = f'!f() {{ test "$1" = get && printf "username=%s\\npassword=%s\\n" "${user_var}" "${password_var}"; }}; f'
        scope = f"credential.{remote.base_url.rstrip('/')}"
        # git-lfs drops the URL path from credential lookups unless told otherwise,
        # which would stop path scoped helpers from matching.
        config += [(f"{scope}.helper", helper), (f"{scope}.useHttpPath", "true")]
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _uses_lfs(repo_dir: str, env: dict[str, str]) -> bool:
    """Detect LFS from .gitattributes on any ref, without requiring git-lfs."""
    refs = _run(["git", "for-each-ref", "--format=%(objectname)"], env, cwd=repo_dir).split()
    for ref in set(refs):
        result = subprocess.run(
            ["git", "cat-file", "-p", f"{ref}:.gitattributes"],
            cwd=repo_dir, env=env, capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and "filter=lfs" in result.stdout:
            return True
    return False


def _run(cmd: list[str], env: dict[str, str], cwd: str | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GitError(f"{' '.join(cmd[:3])} failed: {result.stderr.strip()}")
    return result.stdout
