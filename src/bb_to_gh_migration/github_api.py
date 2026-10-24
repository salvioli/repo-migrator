"""GitHub client built on PyGithub, with retries that never duplicate writes."""

import logging
import re

from github import Auth, Github, GithubRetry, UnknownObjectException
from github.AuthenticatedUser import AuthenticatedUser
from github.Issue import Issue
from github.Organization import Organization
from github.Repository import Repository

from .config import MigrationConfig

logger = logging.getLogger(__name__)

_MARKER = re.compile(r"<!-- bitbucket:([a-z-]+):(\d+) -->")


def marker(kind: str, bitbucket_id: int) -> str:
    """Hidden tag linking a GitHub item to its Bitbucket source, used to resume safely."""
    return f"<!-- bitbucket:{kind}:{bitbucket_id} -->"


def parse_marker(body: str | None) -> tuple | None:
    match = _MARKER.search(body or "")
    return (match.group(1), int(match.group(2))) if match else None


class _SafeRetry(GithubRetry):
    """Retry rate limits on any method, but server errors only on reads.

    A rate limited request was never processed, so repeating it is safe. A 5xx
    on a write may have been applied anyway; resuming handles those instead.
    """

    def is_retry(self, method, status_code, has_retry_after=False):
        if status_code >= 500 and method != "GET":
            return False
        return super().is_retry(method, status_code, has_retry_after)


class GitHubClient:
    def __init__(self, config: MigrationConfig):
        self.config = config
        retry = _SafeRetry(total=10, backoff_factor=2, status_forcelist=list(range(500, 600)) + [429])
        self.client = Github(auth=Auth.Token(config.github_token), base_url=config.gh_api_url, retry=retry)
        self._owner: Organization | AuthenticatedUser | None = None

    def current_login(self) -> str:
        return self.client.get_user().login

    def owner(self) -> Organization | AuthenticatedUser:
        """The target organization, or the authenticated user when GH_ORG names them."""
        if self._owner is None:
            user = self.client.get_user()
            if user.login.lower() == self.config.gh_org.lower():
                self._owner = user
            else:
                self._owner = self.client.get_organization(self.config.gh_org)
        return self._owner

    def repository(self, name: str) -> Repository | None:
        try:
            return self.client.get_repo(f"{self.config.gh_org}/{name}")
        except UnknownObjectException:
            return None

    def create_repository(self, name: str, description: str, private: bool) -> Repository:
        repo = self.owner().create_repo(name=name, description=description, private=private)
        logger.info("Created repository %s", repo.full_name)
        return repo

    @staticmethod
    def is_empty(repo: Repository) -> bool:
        return next(iter(repo.get_branches()), None) is None

    @staticmethod
    def migrated_pull_requests(repo: Repository) -> dict[int, Issue]:
        """Pull requests already migrated, as issues keyed by their Bitbucket id."""
        items = {}
        # The issues endpoint lists pull requests too and exposes the comment API.
        for issue in repo.get_issues(state="all"):
            key = parse_marker(issue.body)
            if issue.pull_request is not None and key and key[0] == "pull-request":
                items[key[1]] = issue
        return items

    @staticmethod
    def migrated_comment_ids(issue: Issue) -> set:
        return {key[1] for key in map(parse_marker, (c.body for c in issue.get_comments())) if key}
