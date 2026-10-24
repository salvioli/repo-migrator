import pytest

from bb_to_gh_migration.bitbucket_api import BitbucketClient
from bb_to_gh_migration.config import ConfigError, MigrationConfig, read_secret
from bb_to_gh_migration.markdown import neutralize_mentions

from .conftest import BB_EMAIL, BB_TOKEN, WORKSPACE


@pytest.mark.parametrize("text, expected", [
    ("thanks @bob!", "thanks `@bob`!"),
    ("cc @{557058:abc-123}", "cc `@{557058:abc-123}`"),
    ("mail bob@example.com", "mail bob@example.com"),
    ("already `@bob` quoted", "already `@bob` quoted"),
    ("```\n@decorator\ndef f(): pass\n```\nhi @ann", "```\n@decorator\ndef f(): pass\n```\nhi `@ann`"),
    ("end of sentence @bob.", "end of sentence `@bob`."),
])
def test_neutralize_mentions(text, expected):
    assert neutralize_mentions(text) == expected


def test_secret_command_runs_without_a_shell(monkeypatch):
    monkeypatch.delenv("TOKEN", raising=False)
    monkeypatch.setenv("TOKEN_CMD", "echo $(whoami); rm -rf /")
    assert read_secret("TOKEN") == "$(whoami); rm -rf /"


def test_failing_secret_command_hides_its_output(monkeypatch):
    monkeypatch.delenv("TOKEN", raising=False)
    monkeypatch.setenv("TOKEN_CMD", "sh -c 'echo partial-secret; echo locked >&2; exit 3'")
    with pytest.raises(ConfigError) as error:
        read_secret("TOKEN")
    assert "exited with 3: locked" in str(error.value)
    assert "partial-secret" not in str(error.value)


def test_pagination_follows_next_links_without_repeating_parameters(e2e):
    for index in range(105):
        e2e.services.add_bb_repo(WORKSPACE, f"repo-{index:03}")
    client = BitbucketClient(MigrationConfig(
        bb_email=BB_EMAIL, bb_api_token=BB_TOKEN, bb_workspace=WORKSPACE, github_token="unused", gh_org="unused",
        bb_api_url=f"{e2e.services.base}/bb-api",
    ))

    slugs = [repo["slug"] for repo in client.repositories()]

    assert len(slugs) == 105
    pages = e2e.services.calls("GET", r"/bb-api/repositories/acme")
    assert len(pages) == 2
    assert all(len(values) == 1 for page in pages for values in page.query.values())
