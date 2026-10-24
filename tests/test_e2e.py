"""End-to-end tests driving the bb2gh CLI against local fake services."""

import pytest

from .conftest import BB_EMAIL, BB_TOKEN, GH_ORG, GH_TOKEN, WORKSPACE, refs

SECONDARY_LIMIT = {"message": "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."}


def comment(comment_id, text, **extra):
    return {"id": comment_id, "content": {"raw": text}, "user": {"display_name": "Grace Hopper"},
            "created_on": "2024-02-03T04:05:06+00:00", **extra}


def assert_no_secret_leaked(e2e, output):
    for secret in (BB_TOKEN, GH_TOKEN):
        assert secret not in output
        assert secret not in e2e.git_argv_log()
    assert not (e2e.root / "global-helper.log").exists()


def test_migrates_refs_lfs_default_branch_and_pull_requests(e2e):
    e2e.make_source_repo("app", mainbranch="develop", lfs=True, branches=["feature"])
    e2e.services.add_bb_pr(WORKSPACE, "app", 7, "feature", "develop", comments=[
        comment(2, "Looks good @bob, ping me at bob@example.com"),
        comment(3, "", deleted=True),
        comment(4, "Rename this", inline={"path": "feature.txt", "to": 1}, parent={"id": 2}),
    ])
    e2e.services.add_bb_pr(WORKSPACE, "app", 8, "feature", "develop", draft=True)
    e2e.services.add_bb_pr(WORKSPACE, "app", 9, "patch", "develop", source_repo="someone/app-fork")

    result = e2e.run("migrate-repo", "app")

    assert result.returncode == 0, result.output
    full = f"{GH_ORG}/app"
    repo = e2e.services.gh_repos[full]
    assert repo["private"] is True
    assert repo["description"] == "app description"
    assert repo["default_branch"] == "develop"
    assert refs(e2e.services.gh_git_path(full)) == refs(e2e.services.root / "bb-git" / WORKSPACE / "app.git")
    assert e2e.services.lfs_objects[f"gh-git/{full}"] == e2e.services.lfs_objects[f"bb-git/{WORKSPACE}/app"]

    prs = e2e.services.gh_items[full]
    assert [(pr["head"], pr["base"], pr["draft"]) for pr in prs] == [("feature", "develop", False), ("feature", "develop", True)]
    assert "<!-- bitbucket:pull-request:7 -->" in prs[0]["body"]
    assert "someone/app-fork" in result.output
    comments = [c["body"] for c in e2e.services.gh_comments[(full, 1)]]
    assert len(comments) == 2
    assert "`@bob`" in comments[0] and "bob@example.com" in comments[0]
    assert "on `feature.txt` line 1, replying to comment 2" in comments[1]

    bb_calls = [r for r in e2e.services.requests if r.path.startswith("/bb-api/")]
    assert {r.auth for r in bb_calls} == {(BB_EMAIL, BB_TOKEN)}
    assert not e2e.services.calls("GET", r"/bb-api/.*/issues")
    assert_no_secret_leaked(e2e, result.output)


def test_rerun_skips_existing_repository_and_resume_finishes_without_duplicates(e2e):
    e2e.make_source_repo("app", branches=["feature"])
    e2e.services.add_bb_pr(WORKSPACE, "app", 1, "feature", "main", comments=[comment(10, "first"), comment(11, "second")])
    # A server error on a write must not be retried blindly: it may have been applied.
    e2e.services.fail("POST", r"/gh-api/repos/target-org/app/issues/1/comments", 502, {"message": "Bad Gateway"})

    first = e2e.run("migrate-repo", "app")
    assert first.returncode == 1
    assert "1 items failed" in first.output
    assert len(e2e.services.calls("POST", r"/gh-api/repos/target-org/app/issues/1/comments")) == 2

    rerun = e2e.run("migrate-repo", "app")
    assert rerun.returncode == 1
    assert "pass --resume" in rerun.output

    resumed = e2e.run("migrate-repo", "--resume", "app")
    assert resumed.returncode == 0, resumed.output
    assert len(e2e.services.gh_items[f"{GH_ORG}/app"]) == 1
    bodies = [c["body"] for c in e2e.services.gh_comments[(f"{GH_ORG}/app", 1)]]
    assert sorted(b.split("\n\n")[1] for b in bodies) == ["first", "second"]


def test_github_secondary_rate_limit_is_retried_once(e2e):
    e2e.make_source_repo("app", branches=["feature"])
    e2e.services.add_bb_pr(WORKSPACE, "app", 1, "feature", "main")
    e2e.services.fail("POST", r"/gh-api/repos/target-org/app/pulls", 403, SECONDARY_LIMIT, {"Retry-After": "1"})

    result = e2e.run("migrate-repo", "app")

    assert result.returncode == 0, result.output
    assert len(e2e.services.calls("POST", r"/gh-api/repos/target-org/app/pulls")) == 2
    assert len(e2e.services.gh_items[f"{GH_ORG}/app"]) == 1


def test_bitbucket_rate_limit_is_retried(e2e):
    e2e.make_source_repo("app")
    e2e.services.fail("GET", r"/bb-api/repositories/acme/app", 429, {"error": {"message": "Rate limit"}},
                      {"Retry-After": "1"})

    result = e2e.run("migrate-repo", "app")

    assert result.returncode == 0, result.output
    assert "retrying in 1s" in result.output


def test_workspace_dry_run_changes_nothing(e2e):
    e2e.make_source_repo("app", branches=["feature"])
    e2e.make_source_repo("lib")
    e2e.services.add_bb_pr(WORKSPACE, "app", 1, "feature", "main")

    result = e2e.run("migrate-workspace", "--dry-run")

    assert result.returncode == 0, result.output
    assert "would create the repository, push all refs and migrate 1 open pull requests" in result.output
    assert "migrate 0 open pull requests" in result.output
    assert not [r for r in e2e.services.requests if r.method != "GET"]
    assert not e2e.services.gh_repos


def test_workspace_continues_after_a_failed_repository(e2e):
    e2e.make_source_repo("broken")
    e2e.make_source_repo("good")
    e2e.services.fail("POST", r"/gh-api/orgs/target-org/repos", 500, {"message": "Server Error"})

    result = e2e.run("migrate-workspace")

    assert result.returncode == 1
    assert "failed" in result.output and "broken" in result.output
    assert set(e2e.services.gh_repos) == {f"{GH_ORG}/good"}
    assert len(e2e.services.calls("POST", r"/gh-api/orgs/target-org/repos")) == 2


def test_migrates_into_personal_account(e2e):
    e2e.make_source_repo("app")

    result = e2e.run("migrate-repo", "app", extra_env={"GH_ORG": e2e.services.gh_login})

    assert result.returncode == 0, result.output
    assert set(e2e.services.gh_repos) == {f"{e2e.services.gh_login}/app"}


def test_test_connection_lists_repositories_without_crawling(e2e):
    e2e.make_source_repo("app")
    e2e.make_source_repo("lib")

    result = e2e.run("test-connection")

    assert result.returncode == 0, result.output
    assert "2 repositories" in result.output
    assert not e2e.services.calls("GET", r".*/pullrequests.*")


@pytest.mark.parametrize("bad_env", [{"GITHUB_TOKEN": "wrong"}, {"BB_API_TOKEN": "wrong"}, {"BB_WORKSPACE": "nope"}])
def test_rejects_bad_credentials(e2e, bad_env):
    e2e.make_source_repo("app")

    result = e2e.run("test-connection", extra_env=bad_env)

    assert result.returncode == 1
    assert "Connection test failed" in result.output


def test_help_needs_no_credentials(e2e):
    result = e2e.run("migrate-repo", "--help", extra_env={"GITHUB_TOKEN": "", "BB_API_TOKEN_CMD": ""})

    assert result.returncode == 0, result.output
    assert "--resume" in result.output
    assert not e2e.services.requests


def test_reports_every_missing_setting(e2e):
    result = e2e.run("test-connection", extra_env={"BB_EMAIL": "", "BB_API_TOKEN_CMD": "", "GH_ORG": ""})

    assert result.returncode == 2
    assert "Missing settings: BB_EMAIL, BB_API_TOKEN, GH_ORG" in result.output
