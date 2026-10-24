# Bitbucket to GitHub Migration Tool

A Python tool that migrates repositories and their open pull requests from a Bitbucket Cloud workspace to a GitHub organization or user account, preserving as much metadata as possible.

## Features

- Full repository migration: every branch and tag, plus Git LFS objects
- Default branch carried over from Bitbucket
- Open pull requests with their description and comments, including file and line context for inline comments
- `@mentions` in migrated text are neutralized so GitHub does not notify unrelated users
- Safe reruns: an interrupted migration can be resumed without creating duplicates
- Dry-run mode that reports what would be migrated without changing anything
- Rate limits on both sides are respected and retried automatically

## Prerequisites

- Python 3.10 or higher
- Git, and Git LFS for repositories that use it
- PDM (recommended for dependency management)

## Installation

```bash
pdm install
```

## Configuration

### Required Credentials

1. **Bitbucket**: an [Atlassian API token](https://support.atlassian.com/bitbucket-cloud/docs/using-api-tokens/) with these scopes:
   - `read:repository:bitbucket`
   - `read:pullrequest:bitbucket`

2. **GitHub**: a personal access token.
   - Classic token: `repo` and `workflow` (`workflow` is needed to push repositories containing `.github/workflows`)
   - Fine-grained token: Administration, Contents, Pull requests and Workflows, all with write access

### Settings

Settings are read from the environment, or from a `.env` file in the current directory. Copy `.env.example` to get started.

| Setting | Description |
| --- | --- |
| `BB_EMAIL` | Atlassian account e-mail |
| `BB_WORKSPACE` | Bitbucket workspace to migrate from |
| `BB_API_TOKEN` or `BB_API_TOKEN_CMD` | Bitbucket API token, or a command printing it |
| `GH_ORG` | GitHub organization, or your own user name, to migrate into |
| `GITHUB_TOKEN` or `GITHUB_TOKEN_CMD` | GitHub token, or a command printing it |

`BB_EMAIL`, `BB_WORKSPACE` and `GH_ORG` can also be given as `--bb-email`, `--bb-workspace` and `--gh-org` before the command name. Secrets are only accepted from the environment, so they never end up in shell history.

`GH_API_URL` and `GH_GIT_URL` point the tool at GitHub Enterprise Server, for example `https://github.example.com/api/v3` and `https://github.example.com`.

#### Secure Credential Storage (Recommended)

The `*_CMD` settings fetch secrets from a password manager instead of storing them in plain text. The command runs without a shell, so pipes and `$(...)` are not supported:

```bash
# pass
BB_API_TOKEN_CMD=pass show bitbucket/api-token

# macOS keychain
GITHUB_TOKEN_CMD=security find-generic-password -a github-migration -w

# GPG encrypted file
BB_API_TOKEN_CMD=gpg --quiet --decrypt /path/to/bitbucket_token.gpg

# 1Password CLI
GITHUB_TOKEN_CMD=op read "op://Private/GitHub Token/credential"
```

Credentials are handed to git through a credential helper scoped to each remote, so they never appear in URLs, command lines or logs, and are never stored by your own git credential helpers.

## Usage

### Testing Connections

```bash
pdm run test-connection
```

Use `pdm run bb2gh --verbose test-connection` for debug logging.

### Migrating Repositories

```bash
pdm run migrate-repo --dry-run repository-name other-repository
pdm run migrate-repo repository-name other-repository
```

### Migrating the Whole Workspace

```bash
pdm run migrate-workspace --dry-run
pdm run migrate-workspace
```

Every repository is attempted even if an earlier one fails, and a summary is printed at the end. The exit code is non-zero when anything failed or was skipped.

### Resuming

A repository that already exists on GitHub and has content is skipped. After an interrupted run, migrate it again with `--resume`: refs are pushed again, and pull requests and comments that were already migrated are left alone.

`--resume` force pushes every Bitbucket ref and deletes GitHub refs that do not exist on Bitbucket, so only use it on repositories created by this tool.

## Limitations

- Bitbucket Cloud no longer provides Issues or Wikis, so neither can be migrated
- Only open pull requests are migrated. Pull requests from forks are skipped, because their branches are not part of the repository
- GitHub assigns new pull request numbers, so `#N` references in migrated text may point to different items
- Reviewers, approvals, tasks and attachments are not migrated. Pull requests and comments are created by the token owner, with the original author and date in a header
- GitHub rejects pushes containing files over 100 MB that are not stored in Git LFS

## Development

```bash
pdm install -G test -G lint
pdm run test
pdm run lint
```

The tests run the CLI end to end against local fakes of the Bitbucket and GitHub APIs, including real git and Git LFS transfers, so they need neither network access nor credentials.

## Contributing

1. Fork the repository
1. Create your feature branch (`git checkout -b feature/AmazingFeature`)
1. Make your changes
1. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
1. Push to the branch (`git push origin feature/AmazingFeature`)
1. Open a Pull Request

## License

MIT License. See the [LICENSE](LICENSE) file for details.

## Support

For issues and feature requests, please use the GitHub issue tracker.

## Authors

- Federico Salvioli: *initial work* ([GitHub](https://github.com/salvioli))
