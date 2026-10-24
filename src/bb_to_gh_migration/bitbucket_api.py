"""Read-only Bitbucket Cloud REST client."""

import logging
from collections.abc import Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import MigrationConfig

logger = logging.getLogger(__name__)

TIMEOUT = (10, 60)


class _LoggingRetry(Retry):
    def sleep(self, response=None):
        wait = (self.get_retry_after(response) if response else None) or self.get_backoff_time()
        if wait:
            logger.warning("Bitbucket request failed or was rate limited, retrying in %.0fs", wait)
        super().sleep(response)


class BitbucketClient:
    def __init__(self, config: MigrationConfig):
        self.config = config
        self.session = requests.Session()
        self.session.auth = (config.bb_email, config.bb_api_token)
        retry = _LoggingRetry(
            total=8,
            backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def _url(self, path: str) -> str:
        return f"{self.config.bb_api_url.rstrip('/')}/{path}"

    def _get(self, url: str, params: dict | None = None) -> dict | None:
        """GET a resource, returning None when it does not exist."""
        response = self.session.get(url, params=params, timeout=TIMEOUT)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        # The "next" link already carries every query parameter of the first page.
        data = self._get(self._url(path), params)
        while data:
            yield from data.get("values", [])
            next_url = data.get("next")
            data = self._get(next_url) if next_url else None

    def workspace_accessible(self) -> bool:
        """Whether the token can list the workspace's repositories, the only scope the tool needs to start."""
        return self._get(self._url(f"repositories/{self.config.bb_workspace}"), {"pagelen": 1}) is not None

    def repositories(self) -> list[dict]:
        return list(self._paginate(f"repositories/{self.config.bb_workspace}", {"pagelen": 100}))

    def repository(self, slug: str) -> dict | None:
        return self._get(self._url(f"repositories/{self.config.bb_workspace}/{slug}"))

    def open_pull_requests(self, slug: str) -> list[dict]:
        prs = self._paginate(
            f"repositories/{self.config.bb_workspace}/{slug}/pullrequests", {"state": "OPEN", "pagelen": 50}
        )
        return sorted(prs, key=lambda pr: pr["id"])

    def pull_request_comments(self, slug: str, pr_id: int) -> list[dict]:
        comments = self._paginate(
            f"repositories/{self.config.bb_workspace}/{slug}/pullrequests/{pr_id}/comments", {"pagelen": 100}
        )
        return [c for c in comments if not c.get("deleted") and (c.get("content") or {}).get("raw")]
