import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from .fake_services import FakeServices

BB_EMAIL = "migrator@example.com"
# Characters that break naive URL embedding are deliberate.
BB_TOKEN = "ATATT-bb/secret#with?odd@chars"
GH_TOKEN = "fake-github-token-for-tests"
WORKSPACE = "acme"
GH_ORG = "target-org"
BB2GH = Path(sys.executable).parent / "bb2gh"
REAL_GIT = shutil.which("git")


@dataclass
class Run:
    returncode: int
    output: str


@dataclass
class Env:
    services: FakeServices
    root: Path
    env: dict[str, str]

    def run(self, *args: str, extra_env: dict[str, str] | None = None) -> Run:
        result = subprocess.run(
            [str(BB2GH), *args], cwd=self.root, env={**self.env, **(extra_env or {})},
            capture_output=True, text=True, timeout=300, check=False,
        )
        return Run(result.returncode, result.stdout + result.stderr)

    def git_argv_log(self) -> str:
        log = self.root / "git-argv.log"
        return log.read_text() if log.exists() else ""

    def make_source_repo(self, slug: str, mainbranch: str = "main", lfs: bool = False, branches: list[str] = ()) -> None:
        """Create a Bitbucket repository with history on several branches and a tag."""
        bare = self.services.add_bb_repo(WORKSPACE, slug, mainbranch=mainbranch)
        work = self.root / "work" / slug
        git = lambda *a: subprocess.run([REAL_GIT, *a], cwd=work, env=self.env, check=True, capture_output=True)
        work.mkdir(parents=True)
        git("init", "--quiet", "-b", mainbranch)
        (work / "README.md").write_text(f"# {slug}\n")
        git("add", "README.md")
        if lfs:
            content = b"\x00binary payload for " + slug.encode()
            oid = self.services.add_lfs_object("bb-git", f"{WORKSPACE}/{slug}", content)
            (work / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
            (work / "asset.bin").write_text(
                f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(content)}\n"
            )
            # Commit the pointer as is, without running LFS filters.
            git("-c", "filter.lfs.process=", "-c", "filter.lfs.clean=cat", "-c", "filter.lfs.required=false",
                "add", ".gitattributes", "asset.bin")
        git("commit", "--quiet", "-m", "initial")
        git("tag", "-a", "v1.0", "-m", "release")
        for branch in branches:
            git("checkout", "--quiet", "-b", branch, mainbranch)
            (work / f"{branch}.txt").write_text(branch)
            git("add", ".")
            git("commit", "--quiet", "-m", f"work on {branch}")
        git("push", "--quiet", "--mirror", str(bare))


def refs(path: Path) -> str:
    return subprocess.run(
        [REAL_GIT, "for-each-ref", "--format=%(objectname) %(refname)"], cwd=path,
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def e2e(tmp_path: Path):
    (tmp_path / "server").mkdir()
    services = FakeServices(tmp_path / "server", (BB_EMAIL, BB_TOKEN), ("x-bitbucket-api-token-auth", BB_TOKEN), GH_TOKEN)
    root = tmp_path / "client"
    root.mkdir()
    # A wrapper records every git command line, including those run by git-lfs.
    bin_dir = root / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(root / "git-argv.log"))}\n'
        f'exec {shlex.quote(REAL_GIT)} "$@"\n'
    )
    (bin_dir / "git").chmod(0o755)
    # A global helper, like osxkeychain, that offers another account's
    # credentials and records every operation git asks of it.
    helper = root / "global-helper"
    helper.write_text(
        f'#!/bin/sh\necho "$1" >> {shlex.quote(str(root / "global-helper.log"))}\n'
        'test "$1" = get && printf "username=intruder\\npassword=wrong\\n"\nexit 0\n'
    )
    helper.chmod(0o755)
    global_config = root / "gitconfig"
    global_config.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        f"[credential]\n\thelper = {helper}\n"
    )
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("BB_", "GH_", "GITHUB_", "GIT_"))
    }
    env.update({
        "HOME": str(root),
        "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        "GIT_CONFIG_GLOBAL": str(global_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "BB_EMAIL": BB_EMAIL,
        "BB_API_TOKEN_CMD": f"printf %s {shlex.quote(BB_TOKEN)}",
        "BB_WORKSPACE": WORKSPACE,
        "GITHUB_TOKEN": GH_TOKEN,
        "GH_ORG": GH_ORG,
        "BB_API_URL": f"{services.base}/bb-api",
        "BB_GIT_URL": f"{services.base}/bb-git",
        "GH_API_URL": f"{services.base}/gh-api",
        "GH_GIT_URL": f"{services.base}/gh-git",
    })
    with services:
        yield Env(services, root, env)
