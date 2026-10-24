"""Migrate Bitbucket Cloud repositories and their open pull requests to GitHub."""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import requests
from github import GithubException
from github.Issue import Issue
from github.PullRequest import PullRequest
from github.Repository import Repository

from . import git_mirror
from .bitbucket_api import BitbucketClient
from .config import MigrationConfig
from .github_api import GitHubClient, marker
from .markdown import neutralize_mentions

logger = logging.getLogger(__name__)

# Bitbucket API tokens authenticate git over HTTPS with this fixed username.
BITBUCKET_GIT_USERNAME = "x-bitbucket-api-token-auth"
GITHUB_GIT_USERNAME = "x-access-token"


@dataclass
class RepoResult:
    slug: str
    status: str  # "migrated", "skipped", "failed" or "dry-run"
    detail: str = ""
    item_errors: int = 0

    @property
    def ok(self) -> bool:
        return self.status in ("migrated", "dry-run") and not self.item_errors


class Migrator:
    def __init__(
        self,
        config: MigrationConfig,
        bitbucket: BitbucketClient | None = None,
        github: GitHubClient | None = None,
    ):
        self.config = config
        self.bb = bitbucket or BitbucketClient(config)
        self.gh = github or GitHubClient(config)

    def check_connections(self) -> bool:
        ok = True
        try:
            if self.bb.workspace_accessible():
                logger.info("Connected to Bitbucket workspace %s", self.config.bb_workspace)
            else:
                logger.error("Bitbucket workspace %s not found or not accessible", self.config.bb_workspace)
                ok = False
        except requests.RequestException as e:
            logger.error("Failed to connect to Bitbucket: %s", e)
            ok = False
        try:
            logger.info("Connected to GitHub as %s", self.gh.current_login())
            logger.info("Migrating into GitHub account %s", self.gh.owner().login)
        except (GithubException, requests.RequestException) as e:
            logger.error("Failed to connect to GitHub: %s", e)
            ok = False
        return ok

    def workspace_repositories(self) -> list[str]:
        return [repo["slug"] for repo in self.bb.repositories()]

    def migrate(self, slugs: Iterable[str], dry_run: bool = False, resume: bool = False) -> list[RepoResult]:
        """Migrate each repository, continuing past failures."""
        slugs = list(slugs)
        results = []
        for index, slug in enumerate(slugs, 1):
            logger.info("[%d/%d] %s", index, len(slugs), slug)
            try:
                result = self._migrate_repository(slug, dry_run, resume)
            except Exception as e:
                logger.debug("Migration of %s failed", slug, exc_info=True)
                result = RepoResult(slug, "failed", f"{type(e).__name__}: {e}")
            log = logger.info if result.ok else logger.error
            log("%s: %s %s", slug, result.status, result.detail)
            results.append(result)
        return results

    def _migrate_repository(self, slug: str, dry_run: bool, resume: bool) -> RepoResult:
        details = self.bb.repository(slug)
        if details is None:
            return RepoResult(slug, "failed", "not found on Bitbucket")
        target = self.gh.repository(slug)
        if target is not None and not resume and not self.gh.is_empty(target):
            return RepoResult(slug, "skipped", "already exists on GitHub, pass --resume to continue migrating into it")

        prs = self.bb.open_pull_requests(slug)
        if dry_run:
            action = "reuse" if target is not None else "create"
            return RepoResult(
                slug, "dry-run",
                f"would {action} the repository, push all refs and migrate {len(prs)} open pull requests",
            )

        existing: dict[int, Issue] = {}
        if target is None:
            target = self.gh.create_repository(slug, details.get("description") or "", details.get("is_private", True))
        else:
            existing = self.gh.migrated_pull_requests(target)

        git_mirror.mirror(
            git_mirror.Remote(
                self.config.bb_git_url, f"{self.config.bb_workspace}/{slug}",
                BITBUCKET_GIT_USERNAME, self.config.bb_api_token,
            ),
            git_mirror.Remote(
                self.config.gh_git_url, f"{self.config.gh_org}/{slug}",
                GITHUB_GIT_USERNAME, self.config.github_token,
            ),
        )
        main_branch = (details.get("mainbranch") or {}).get("name")
        if main_branch:
            target.edit(default_branch=main_branch)

        errors = self._migrate_pull_requests(slug, target, prs, existing)
        detail = f"{len(prs)} open pull requests"
        if errors:
            detail += f", {errors} items failed, rerun with --resume to retry them"
        return RepoResult(slug, "migrated", detail, errors)

    def _migrate_pull_requests(self, slug: str, target: Repository, prs: list[dict], existing: dict[int, Issue]) -> int:
        errors = 0
        own_repo = f"{self.config.bb_workspace}/{slug}".lower()
        for pr in prs:
            try:
                gh_item = existing.get(pr["id"])
                if gh_item is not None:
                    create_comment, done = gh_item.create_comment, self.gh.migrated_comment_ids(gh_item)
                else:
                    source_repo = (pr.get("source", {}).get("repository") or {}).get("full_name", "")
                    if source_repo.lower() != own_repo:
                        logger.warning("Skipping pull request #%d: its branch lives in fork %s", pr["id"], source_repo)
                        continue
                    header = (
                        f"> Migrated from Bitbucket pull request [#{pr['id']}]({_link(pr)}), "
                        f"opened by **{_name(pr.get('author'))}** on {pr.get('created_on', 'unknown date')}."
                    )
                    gh_pr: PullRequest = target.create_pull(
                        title=pr.get("title") or "Untitled",
                        body=_body(header, pr, marker("pull-request", pr["id"]), text_field="description"),
                        head=pr["source"]["branch"]["name"],
                        base=pr["destination"]["branch"]["name"],
                        draft=bool(pr.get("draft")),
                    )
                    create_comment, done = gh_pr.create_issue_comment, set()
                comments = self.bb.pull_request_comments(slug, pr["id"])
                errors += self._post_comments(create_comment, comments, done)
            except GithubException as e:
                logger.error("Failed to migrate pull request #%d: %s", pr["id"], e)
                errors += 1
        return errors

    @staticmethod
    def _post_comments(create_comment: Callable[[str], object], comments: list[dict], done: set) -> int:
        errors = 0
        for comment in sorted(comments, key=lambda c: c["id"]):
            if comment["id"] in done:
                continue
            context = ""
            inline = comment.get("inline")
            if inline:
                line = inline.get("to") or inline.get("from")
                context = f" on `{inline.get('path')}`" + (f" line {line}" if line else "")
            if comment.get("parent"):
                context += f", replying to comment {comment['parent']['id']}"
            header = f"> **{_name(comment.get('user'))}** commented{context} on {comment.get('created_on', 'unknown date')}."
            try:
                create_comment(_body(header, comment, marker("comment", comment["id"])))
            except GithubException as e:
                logger.error("Failed to migrate comment %d: %s", comment["id"], e)
                errors += 1
        return errors


def _name(user: dict | None) -> str:
    return (user or {}).get("display_name") or "Unknown user"


def _link(item: dict) -> str:
    return item.get("links", {}).get("html", {}).get("href", "")


def _body(header: str, item: dict, tag: str, text_field: str = "content") -> str:
    value = item.get(text_field)
    text = value.get("raw") if isinstance(value, dict) else value
    return f"{header}\n\n{neutralize_mentions(text or '')}\n\n{tag}"
