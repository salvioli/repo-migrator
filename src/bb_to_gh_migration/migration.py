import requests
import time
import sys
from datetime import datetime, timedelta  # Import time module for sleep
from github import Github, RateLimitExceededException
from functools import wraps

# from atlassian.bitbucket.cloud import Bitbucket
import git
import tempfile
import logging
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
import json

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ANSI_CLEAR_LINE = '\x1b[2K'

@dataclass
class MigrationConfig:
    bb_username: str
    bb_password: str
    github_token: str
    bb_workspace: str
    gh_org: str
    dry_run: bool = False
    verbose: bool = False


def countdown(seconds: int) -> None:
    """Display an interactive countdown timer with ETA"""
    next_attempt_time = datetime.now() + timedelta(seconds=seconds)
    time_str = time.strftime("%H:%M:%S", time.gmtime(seconds))
    sys.stdout.write(f"Waiting for {time_str}\n")
    sys.stdout.flush()
    while datetime.now() < next_attempt_time:
        remaining = (next_attempt_time - datetime.now()).seconds
        next_time_str = next_attempt_time.strftime("%H:%M:%S")
        sys.stdout.write(f"{ANSI_CLEAR_LINE}\rWaiting {remaining}s for rate limit (next attempt at {next_time_str})")
        sys.stdout.flush()
        time.sleep(0.2)
    sys.stdout.write(f"{ANSI_CLEAR_LINE}\r")
    sys.stdout.flush()

def exponential_backoff(max_retries=5, base_delay=10):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.RetryRequest as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Max retries ({max_retries}) exceeded: {str(e)}")
                        raise
                    delay = (2 ** attempt) * base_delay
                    logger.warning(f"API rate limit hit. Retry {attempt + 1}/{max_retries}")
                    countdown(delay)
            return None
        return wrapper
    return decorator


class BitbucketConnector:
    def __init__(self, config: MigrationConfig):
        self.config = config
        self.base_url = "https://api.bitbucket.org/2.0"
        self.auth = (self.config.bb_username, self.config.bb_password)
        self.session = requests.Session()
        self.session.auth = self.auth

    @exponential_backoff(max_retries=5, base_delay=1)
    def _make_request(self, method: str, url: str, params: Dict = None, data: Dict = None) -> Optional[Dict]:
        """Helper method to make API requests with exponential backoff"""
        try:
            response = self.session.request(method, url, params=params, json=data)
            
            # Status codes that should not trigger retries
            if response.status_code == 404:  # Resource not found
                logger.info(f"Resource not found (404): {url}")
                return None
            elif response.status_code == 400:  # Bad request
                logger.error(f"Bad request (400): {url}")
                response.raise_for_status()
            elif response.status_code == 401:  # Unauthorized
                logger.error(f"Unauthorized (401): {url}")
                response.raise_for_status()
            
            # Status codes that should trigger retries
            elif response.status_code in [403, 429, 500, 502, 503, 504]:
                # 403: Rate limit, 429: Too many requests, 5xx: Server errors
                logger.warning(f"Received status code {response.status_code}, will retry: {url}")
                raise requests.exceptions.RetryRequest(response=response)
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RetryRequest:
            raise
        except Exception as e:
            logger.error(f"Request failed: {e}")
            raise

    def test_connection(self) -> bool:
        """Test Bitbucket connection and permissions"""
        try:
            url = f"{self.base_url}/user"
            response = requests.get(url, auth=self.auth, timeout=15)  # Add timeout here
            response.raise_for_status()
            user_data = response.json()
            logger.info(
                f"Successfully connected to Bitbucket Cloud as {user_data['display_name']} ({user_data['username']})"
            )
            if self.config.verbose:
                logger.info(f"User UUID: {user_data['uuid']}")
                logger.info(f"Account ID: {user_data['account_id']}")
                logger.info(f"Bitbucket workspace: {self.config.bb_workspace}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Bitbucket Cloud: {str(e)}")
            return False

    def get_repositories(self) -> List[Dict]:
        """Get all repositories in the workspace"""
        repos = []
        url = f"{self.base_url}/repositories/{self.config.bb_workspace}"
        while url:
            data = self._make_request('GET', url)
            if not data:
                break
            repos.extend(data.get("values", []))
            url = data.get("next")
        if self.config.verbose:
            repo_names = [repo["slug"] for repo in repos]
            logger.info(f"Retrieved repositories: {repo_names}")
        return repos

    def get_repository_details(self, repo_slug: str) -> Optional[Dict]:
        """Get detailed information about a specific repository"""
        url = f"{self.base_url}/repositories/{self.config.bb_workspace}/{repo_slug}"
        repo = self._make_request('GET', url)
        if repo and self.config.verbose:
            logger.info(
                f"Repository details: {json.dumps(repo, indent=2, sort_keys=True)}"
            )
        return repo

    def get_issues(self, repo_slug: str) -> List[Dict]:
        """Get all issues for a repository"""
        issues = []
        url = f"{self.base_url}/repositories/{self.config.bb_workspace}/{repo_slug}/issues"
        data = self._make_request('GET', url)
        if data is None:  # Repository doesn't have issues enabled
            logger.info(f"Issues are not enabled for repository {repo_slug}")
            return []
            
        while data:
            issues.extend(data.get("values", []))
            url = data.get("next")
            if not url:
                break
            data = self._make_request('GET', url)
            if data:
                logger.info(f"Retrieved {len(issues)} issues for {repo_slug}")
            
        if self.config.verbose:
            logger.info(f"Issues for {repo_slug}: {json.dumps(issues, indent=2, sort_keys=True)}")
        return issues

    def get_pull_requests(self, repo_slug: str) -> List[Dict]:
        """Get all open pull requests for a repository, including comments"""
        prs = []
        url = f"{self.base_url}/repositories/{self.config.bb_workspace}/{repo_slug}/pullrequests"
        params = {'state': 'OPEN', 'pagelen': 50}  # Only fetch open PRs
        
        while url:
            data = self._make_request('GET', url, params=params)
            if not data:
                break
            for pr in data.get("values", []):
                pr_id = pr.get('id')
                pr_comments = self.get_pull_request_comments(repo_slug, pr_id)
                pr['comments'] = pr_comments
                prs.append(pr)
            url = data.get("next")
        if self.config.verbose:
            logger.info(f"Total open PRs retrieved: {len(prs)}")
        return prs

    def get_pull_request_comments(self, repo_slug: str, pr_id: int) -> List[Dict]:
        """Get all comments for a specific pull request"""
        comments = []
        url = f"{self.base_url}/repositories/{self.config.bb_workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
        while url:
            data = self._make_request('GET', url)
            if not data:
                break
            comments.extend(data.get("values", []))
            url = data.get("next")
        if self.config.verbose:
            logger.info(f"Retrieved {len(comments)} comments for PR #{pr_id}")
        return comments

    def get_clone_url(self, repo_slug: str) -> str:
        """Get HTTPS clone URL with auth embedded"""
        return f"https://{self.config.bb_username}:{self.config.bb_password}@bitbucket.org/{self.config.bb_workspace}/{repo_slug}.git"


class GitHubConnector:
    def __init__(self, config: MigrationConfig):
        self.config = config
        self.client = self._setup_client()
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"token {self.config.github_token}"})

    def _setup_client(self) -> Github:
        try:
            # Disable PyGithub's retry mechanism by setting retry=False
            client = Github(self.config.github_token, retry=False)
            # Test connection
            client.get_user().login
            logger.info("Successfully connected to GitHub")
            return client
        except Exception as e:
            logger.error(f"Failed to connect to GitHub: {str(e)}")
            raise

    @exponential_backoff(max_retries=5, base_delay=1)
    def _make_request(self, func):
        """Execute GitHub API calls with exponential backoff"""
        return func()

    def test_connection(self) -> bool:
        """Test GitHub connection and permissions"""
        try:
            user = self.client.get_user()
            org = self.client.get_organization(self.config.gh_org)
            logger.info(f"Successfully connected to GitHub as {user.login}")
            logger.info(f"Access to organization: {org.login}")
            if self.config.verbose:
                logger.info(f"User ID: {user.id}")
                logger.info(f"Organization ID: {org.id}")
            return True
        except Exception as e:
            logger.error(f"Failed to access organization: {str(e)}")
            return False

    @exponential_backoff(max_retries=5, base_delay=1)
    def create_repository(
        self, name: str, description: str, private: bool
    ) -> Optional[Dict]:
        """Create a new repository with error handling"""
        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would create repository: {name}")
            return {"name": name}
        org = self.client.get_organization(self.config.gh_org)
        def create_repo():
            return org.create_repo(name=name, description=description, private=private)
        repo = self._make_request(create_repo)
        if repo:
            logger.info(f"Created repository: {repo.full_name}")
        return repo

    def get_clone_url(self, repo_name: str) -> str:
        """Get HTTPS clone URL with auth embedded"""
        return f"https://{self.config.github_token}@github.com/{self.config.gh_org}/{repo_name}.git"

    @exponential_backoff(max_retries=5, base_delay=1)
    def create_issue(self, repo_name: str, issue_data: Dict) -> Optional[Dict]:
        """Create a new issue with error handling"""
        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would create issue: {issue_data.get('title')}")
            return None
        org = self.client.get_organization(self.config.gh_org)
        repo = org.get_repo(repo_name)
        def create_issue():
            title = issue_data.get('title', 'No title')
            body = f"""Migrated from Bitbucket
Original Reporter: {issue_data.get('reporter', {}).get('display_name', 'Unknown')}
Original Link: {issue_data.get('links', {}).get('html', {}).get('href', '')}
Original State: {issue_data.get('state', 'Unknown')}

{issue_data.get('content', {}).get('raw', '')}"""
            issue = repo.create_issue(
                title=title,
                body=body,
                labels=['migrated-from-bitbucket', issue_data.get('state', 'Unknown')],
            )
            state_mapping = {'new': 'open', 'open': 'open', 'resolved': 'closed', 'closed': 'closed'}
            out_state = state_mapping.get(issue_data.get('state'), 'open')
            issue.edit(state=out_state)
            return issue
        issue = self._make_request(create_issue)
        if issue:
            logger.info(f"Created issue: {issue.title}")
        return issue

    @exponential_backoff(max_retries=5, base_delay=1)
    def create_pull_request(self, repo_name: str, pr_data: Dict) -> Optional[Dict]:
        try:
            if self.config.dry_run:
                logger.info(f"[DRY RUN] Would create pull request: {pr_data.get('title')}")
                return None

            org = self.client.get_organization(self.config.gh_org)
            repo = org.get_repo(repo_name)

            # Get branch information
            source_branch = pr_data.get('source', {}).get('branch', {}).get('name')
            target_branch = pr_data.get('destination', {}).get('branch', {}).get('name')

            if not source_branch or not target_branch:
                logger.error(f"Missing branch information for PR: {pr_data.get('title')}")
                return None

            # Create pull request
            pr = repo.create_pull(
                title=pr_data.get("title", "No title"),
                body=self._format_pr_body(pr_data),
                head=source_branch,
                base=target_branch
            )

            # Handle comments
            self._add_pr_comments(pr, pr_data.get('comments', []))

            logger.info(f"Created pull request: {pr.title}")
            return pr

        except Exception as e:
            logger.error(f"Failed to create pull request {pr_data.get('title')}: {str(e)}")
            return None

    def _add_pr_comments(self, pr, comments: List[Dict]) -> None:
        """Add comments to a pull request"""
        if not comments:
            return
        
        logger.info(f"Migrating {len(comments)} comments")
        for comment in comments:
            try:
                comment_body = f"""Comment by {comment.get('user', {}).get('display_name', 'Unknown')}
Original comment date: {comment.get('created_on', 'Unknown')}

{comment.get('content', {}).get('raw', '')}"""
                pr.create_issue_comment(comment_body)
            except Exception as e:
                logger.warning(f"Failed to create comment: {str(e)}")

    def _format_pr_body(self, pr_data: Dict) -> str:
        """Format pull request description with migration metadata"""
        return f"""Migrated from Bitbucket Pull Request
Original Author: {pr_data.get('author', {}).get('display_name', 'Unknown')}
Original Created On: {pr_data.get('created_on', 'Unknown')}
Original Link: {pr_data.get('links', {}).get('html', {}).get('href', '')}

{pr_data.get('description', '')}"""


class Migrator:
    def __init__(self, config: MigrationConfig):
        self.config = config
        self.bb = BitbucketConnector(config)
        self.gh = GitHubConnector(config)

    def test_connections(self) -> bool:
        """Test both Bitbucket and GitHub connections"""
        logger.info("Testing connections...")
        bb_success = self.bb.test_connection()
        gh_success = self.gh.test_connection()
        return bb_success and gh_success

    def test_repository_listing(self) -> None:
        """Test listing repositories from Bitbucket"""
        logger.info("Testing repository listing...")
        repos = self.bb.get_repositories()
        
        for repo in repos:
            repo_slug = repo['slug']
            logger.info(f"\nRepository: {repo_slug}")
            
            # Get issues
            issues = self.bb.get_issues(repo_slug)
            if self.config.verbose:
                logger.info(f"Issues ({len(issues)}):")
                for issue in issues:
                    logger.info(f"  - [{issue.get('state', 'unknown')}] {issue.get('title', 'No title')}")
            else:
                logger.info(f"Issues: {len(issues)}")
            
            # Get pull requests
            prs = self.bb.get_pull_requests(repo_slug)
            logger.info(f"Pull Requests ({len(prs)}):")
            if self.config.verbose:
                for pr in prs:
                    logger.info(f"  - [{pr.get('state', 'unknown')}] {pr.get('title', 'No title')}")

    def migrate_workspace(self) -> None:
        """Migrate all repositories in the Bitbucket workspace."""
        repos = self.bb.get_repositories()
        for repo in repos:
            repo_slug = repo['slug']
            if self.config.dry_run:
                logger.info(f"[DRY RUN] Would migrate repository: {repo_slug}")
            else:
                logger.info(f"Starting migration for repository: {repo_slug}")
                self.migrate_single_repository(repo_slug)

    def migrate_single_repository(self, repo_slug: str) -> None:
        """Migrate a single repository (test mode if dry_run is True)"""
        mode = "[DRY RUN] " if self.config.dry_run else ""
        logger.info(f"{mode}Starting migration for {repo_slug}")

        # Step 1: Get repository details
        logger.info(f"{mode}Step 1: Getting repository details")
        repo_details = self.bb.get_repository_details(repo_slug)
        if not repo_details:
            logger.error(f"Failed to get details for {repo_slug}. Aborting migration.")
            return

        # Step 2: Get issues
        logger.info(f"{mode}Step 2: Getting issues")
        issues = self.bb.get_issues(repo_slug)
        logger.info(f"Found {len(issues)} issues")

        # Step 3: Get pull requests
        logger.info(f"{mode}Step 3: Getting open pull requests")
        prs = self.bb.get_pull_requests(repo_slug)
        logger.info(f"Found {len(prs)} open pull requests")

        # Step 4: Create repository and migrate content
        if self.config.dry_run:
            logger.info("[DRY RUN] Would create repository and migrate content")
            return

        logger.info("Step 4: Creating repository and migrating content")
        new_repo = self.gh.create_repository(
            name=repo_slug,
            description=repo_details.get("description", ""),
            private=repo_details.get("is_private", True),
        )
        if not new_repo:
            logger.error(f"Failed to create GitHub repository for {repo_slug}. Aborting migration.")
            return

        source_url = self.bb.get_clone_url(repo_slug)
        target_url = self.gh.get_clone_url(repo_slug)
        if not self._migrate_repository_content(repo_slug, source_url, target_url):
            logger.error(f"Failed to migrate repository content for {repo_slug}")
            return

        # Migrate all items
        if not self.config.dry_run:
            logger.info("Step 5: Migrating issues")
            for issue in issues:
                self.gh.create_issue(repo_slug, issue)
            logger.info(f"Migrated {len(issues)} issues")

            # Migrate pull requests
            logger.info("Step 6: Migrating pull requests")
            for pr in prs:
                self.gh.create_pull_request(repo_slug, pr)
            logger.info(f"Migrated {len(prs)} pull requests")

        logger.info(f"Successfully completed migration for {repo_slug}")

    def _migrate_repository_content(self, repo_slug: str, source_url: str, target_url: str) -> bool:
        """Clone repository and push to new remote"""
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                logger.info(f"Cloning repository from Bitbucket...")
                repo = git.Repo.clone_from(source_url, temp_dir, mirror=True)

                logger.info(f"Pushing repository to GitHub...")
                repo.git.push('--mirror', target_url)

                logger.info(f"Successfully migrated repository content")
                return True
            except Exception as e:
                logger.error(f"Failed to migrate repository content: {str(e)}")
                return False
