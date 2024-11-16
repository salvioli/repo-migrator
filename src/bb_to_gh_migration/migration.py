import requests
import time  # Import time module for sleep
from github import Github
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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


@dataclass
class MigrationConfig:
    bb_username: str
    bb_password: str
    github_token: str
    bb_workspace: str
    gh_org: str
    dry_run: bool = False
    verbose: bool = False


class BitbucketConnector:
    def __init__(self, config: MigrationConfig):
        self.config = config
        self.base_url = "https://api.bitbucket.org/2.0"
        self.auth = (self.config.bb_username, self.config.bb_password)
        self.session = self._get_session_with_retries()

    def _get_session_with_retries(self) -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=1,  # Exponential backoff factor (in seconds)
            status_forcelist=[403, 429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"]
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.auth = self.auth
        return session

    def _make_request(self, method: str, url: str, params: Dict = None, data: Dict = None) -> Optional[Dict]:
        """Helper method to make API requests with retry logic."""
        try:
            response = self.session.request(method, url, params=params, json=data)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Request failed: {e}")
            return None

    def test_connection(self) -> bool:
        """Test Bitbucket connection and permissions"""
        try:
            url = f"{self.base_url}/user"
            response = requests.get(url, auth=self.auth)
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
        while url:
            data = self._make_request('GET', url)
            if not data:
                break
            issues.extend(data.get("values", []))
            url = data.get("next")
            logger.info(f"Retrieved {len(issues)} issues for {repo_slug}")
        if self.config.verbose:
            logger.info(f"Issues for {repo_slug}: {json.dumps(issues, indent=2, sort_keys=True)}")
        return issues

    def get_pull_requests(self, repo_slug: str) -> List[Dict]:
        """Get all pull requests for a repository, including comments"""
        prs = []
        url = f"{self.base_url}/repositories/{self.config.bb_workspace}/{repo_slug}/pullrequests"
        params = {'state': 'ALL', 'pagelen': 50}  # Increase page size and ensure pagination
        
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
            logger.info(f"Total PRs retrieved: {len(prs)}")
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
        self.session = self._get_session_with_retries()

    def _setup_client(self) -> Github:
        try:
            client = Github(self.config.github_token)
            # Test connection
            client.get_user().login
            logger.info("Successfully connected to GitHub")
            return client
        except Exception as e:
            logger.error(f"Failed to connect to GitHub: {str(e)}")
            raise

    def _get_session_with_retries(self) -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=1,  # Exponential backoff factor (in seconds)
            status_forcelist=[403, 429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"]
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.headers.update({"Authorization": f"token {self.config.github_token}"})
        return session

    def _make_request(self, func):
        try:
            return func()
        except Exception as e:
            logger.error(f"Request failed: {e}")
            return None

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

    def create_pull_request(self, repo_name: str, pr_data: Dict) -> Optional[Dict]:
        """Create a new pull request with comments"""
        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would create pull request: {pr_data.get('title')}")
            return None
        org = self.client.get_organization(self.config.gh_org)
        repo = org.get_repo(repo_name)
        def create_pr():
            title = pr_data.get("title", "No title")
            body = f"""Migrated from Bitbucket Pull Request
Original Author: {pr_data.get('author', {}).get('display_name', 'Unknown')}
Original Created On: {pr_data.get('created_on', 'Unknown')}
Original Link: {pr_data.get('links', {}).get('html', {}).get('href', '')}

{pr_data.get('description', '')}"""
            source_branch = pr_data.get('source', {}).get('branch', {}).get('name')
            target_branch = pr_data.get('destination', {}).get('branch', {}).get('name')
            if not source_branch or not target_branch:
                logger.error(f"Missing branch information for PR: {title}")
                return None
            pr = repo.create_pull(title=title, body=body, head=source_branch, base=target_branch)
            # Handle comments, assignees, and PR state here...
            return pr
        pr = self._make_request(create_pr)
        if pr:
            logger.info(f"Created pull request: {pr.title}")
        return pr

    def create_pull_request(self, repo_name: str, pr_data: Dict) -> Optional[Dict]:
        """Create a new pull request with comments"""
        try:
            if self.config.dry_run:
                logger.info(f"[DRY RUN] Would create pull request: {pr_data.get('title')}")
                return None

            org = self.client.get_organization(self.config.gh_org)
            repo = org.get_repo(repo_name)

            title = pr_data.get("title", "No title")
            body = f"""Migrated from Bitbucket Pull Request
Original Author: {pr_data.get('author', {}).get('display_name', 'Unknown')}
Original Created On: {pr_data.get('created_on', 'Unknown')}
Original Link: {pr_data.get('links', {}).get('html', {}).get('href', '')}

{pr_data.get('description', '')}"""

            # Get branch information
            source_branch = pr_data.get('source', {}).get('branch', {}).get('name')
            target_branch = pr_data.get('destination', {}).get('branch', {}).get('name')

            if not source_branch or not target_branch:
                logger.error(f"Missing branch information for PR: {title}")
                return None

            pr = repo.create_pull(
                title=title,
                body=body,
                head=source_branch,
                base=target_branch
            )

            # Handle comments
            pr_comments = pr_data.get('comments', [])
            if pr_comments:
                logger.info(f"Migrating {len(pr_comments)} comments for PR: {title}")
                for comment in pr_comments:
                    try:
                        comment_body = f"""Comment by {comment.get('user', {}).get('display_name', 'Unknown')}
Original comment date: {comment.get('created_on', 'Unknown')}

{comment.get('content', {}).get('raw', '')}"""
                        pr.create_issue_comment(comment_body)
                    except Exception as e:
                        logger.warning(f"Failed to create comment: {str(e)}")

            # Handle assignees
            reviewers = pr_data.get('reviewers', [])
            if reviewers:
                assignees = [
                    reviewer.get('user', {}).get('username')
                    for reviewer in reviewers
                    if reviewer.get('user', {}).get('username')
                ]
                if assignees:
                    logger.info(f"Setting assignees for PR: {assignees}")
                    try:
                        pr.add_to_assignees(*assignees)
                    except Exception as e:
                        logger.warning(f"Failed to set assignees {assignees}: {str(e)}")

            # Handle PR state
            if pr_data.get("state") == "MERGED":
                logger.info(f"Original PR was merged. Marking as closed: {title}")
                pr.edit(state="closed")
            elif pr_data.get("state") == "DECLINED":
                logger.info(f"Original PR was declined. Marking as closed: {title}")
                pr.edit(state="closed")

            logger.info(f"Created pull request: {pr.title}")
            return pr

        except Exception as e:
            logger.error(f"Failed to create pull request {pr_data.get('title')}: {str(e)}")
            return None


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
        logger.info(f"{mode}Step 3: Getting pull requests")
        prs = self.bb.get_pull_requests(repo_slug)
        logger.info(f"Found {len(prs)} pull requests")

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
