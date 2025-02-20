# Bitbucket to GitHub Migration Tool

A Python-based tool for migrating repositories, issues, and pull requests from Bitbucket to GitHub. This tool helps teams migrate their entire Bitbucket workspace to a GitHub organization while preserving as much metadata as possible.

## Features

- Full repository migration including all branches and tags
- Issue migration with comments and metadata
- Pull request migration with comments and status
- Dry-run mode for testing
- Verbose logging for debugging
- Incremental testing capabilities
- Error handling and reporting

## Prerequisites

- Python 3.8 or higher
- Git installed and configured
- Admin access to your Bitbucket workspace
- Admin access to your GitHub organization
- PDM installed (recommended for dependency management)

## Installation

1. Install dependencies:

```bash
pdm install        # Install all dependencies including development tools
```

## Configuration

### Required Credentials

1. **Bitbucket**:
   - Username
   - App Password with the following permissions:
     - Account: Read
     - Repositories: Read
     - Issues: Read
     - Pull requests: Read

2. **GitHub**:
   - Personal Access Token with the following permissions:
     - `repo` (Full control of private repositories)
     - `admin:org` (Full control of orgs and teams)
     - `workflow` (Update GitHub Action workflows)

### Environment Variables

The tool supports configuration via environment variables. Create a `.env` file by copying the template:

#### Basic Configuration

```markdown
BB_USERNAME=your-bitbucket-username
BB_PASSWORD=your-password
BB_WORKSPACE=your-bitbucket-workspace
GITHUB_TOKEN=your-github-token
GH_ORG=your-github-org
```

#### Secure Credential Storage (Recommended)

For better security, the tool supports shell command substitution in the .env file. This allows you to fetch credentials from password managers or secure storage:

```markdown
# Use 'pass' password manager
BB_PASSWORD=$(pass show bitbucket/password)

# Use macOS keychain
GITHUB_TOKEN=$(security find-generic-password -a "github-migration" -w)

# Use GPG encrypted file
BB_PASSWORD=$(gpg -d ~/.bitbucket_password.gpg)

# Use 1Password CLI
GITHUB_TOKEN=$(op item get "GitHub Token" --fields credential)
```

## Usage

### Testing Connections

First, test your connections to both platforms:

```bash
pdm run test-connection
```

### Verbose Testing

To see detailed API responses and data:

```bash
pdm run test-connection --verbose
```

### Testing Single Repository

To test migration for a specific repository:

```bash
pdm run migrate-repo \
    --dry-run \
    repository-name
```

### Full Migration

To fully migrate the workspace use the following command:

```bash
pdm run migrate-workspace your_gh_org
```

you can use the `--dry-run` flag in order to only test the migration.

## Limitations

- Pull request merge status cannot be replicated (GitHub API limitation)
- Review comments and approval states are migrated as regular comments
- PR creation will fail if the source branch no longer exists
- GitHub API rate limits may affect large migrations
- Some Bitbucket-specific features may not have GitHub equivalents

## Contributing

1. Fork the repository
1. Create your feature branch (`git checkout -b feature/AmazingFeature`)
1. Make your changes
1. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
1. Push to the branch (`git push origin feature/AmazingFeature`)
1. Open a Pull Request

## License

MIT License - see the [LICENSE](LICENSE) file for details

## Support

For issues and feature requests, please use the GitHub issue tracker.

## Authors

- Federico Salvioli - *Initial work* - [GitHub](https://github.com/salvioli)
