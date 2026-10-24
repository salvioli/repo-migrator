"""In-process fakes of the Bitbucket Cloud and GitHub HTTP services.

A single local server exposes:

* /bb-api/...  the subset of the Bitbucket 2.0 REST API the tool reads
* /gh-api/...  the subset of the GitHub REST API the tool calls
* /bb-git/..., /gh-git/...  real git smart HTTP (via git http-backend) and the
  Git LFS batch API, both behind HTTP basic auth

Every request is recorded so tests can assert on what the tool sent.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

GIT_HTTP_BACKEND = Path(subprocess.check_output(["git", "--exec-path"], text=True).strip()) / "git-http-backend"


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, list[str]]
    auth: tuple[str, str] | None
    body: dict | None


@dataclass
class Fault:
    """Answer the next `times` matching requests with a canned error."""

    method: str
    path_pattern: str
    status: int
    body: dict
    headers: dict[str, str] = field(default_factory=dict)
    times: int = 1


class FakeServices:
    def __init__(self, root: Path, bb_auth: tuple[str, str], bb_git_auth: tuple[str, str], gh_token: str):
        self.root = root
        self.bb_auth = bb_auth
        self.bb_git_auth = bb_git_auth
        self.gh_token = gh_token
        self.gh_login = "migration-bot"
        self.requests: list[Recorded] = []
        self.faults: list[Fault] = []
        self.lock = threading.Lock()
        # Bitbucket state
        self.bb_repos: dict[str, dict] = {}
        self.bb_prs: dict[str, list[dict]] = {}
        self.bb_pr_comments: dict[tuple[str, int], list[dict]] = {}
        # GitHub state
        self.gh_orgs = {"target-org"}
        self.gh_repos: dict[str, dict] = {}
        self.gh_items: dict[str, list[dict]] = {}
        self.gh_comments: dict[tuple[str, int], list[dict]] = {}
        self.lfs_objects: dict[str, dict[str, bytes]] = {}
        (root / "bb-git").mkdir()
        (root / "gh-git").mkdir()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    # Bitbucket fixtures

    def add_bb_repo(self, workspace: str, slug: str, mainbranch: str = "main", private: bool = True) -> Path:
        full = f"{workspace}/{slug}"
        self.bb_repos[full] = {
            "slug": slug,
            "full_name": full,
            "description": f"{slug} description",
            "is_private": private,
            "mainbranch": {"name": mainbranch},
            "links": {"html": {"href": f"https://bitbucket.org/{full}"}},
        }
        self.bb_prs[full] = []
        path = self.root / "bb-git" / workspace / f"{slug}.git"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "--quiet", str(path)], check=True)
        return path

    def add_bb_pr(self, workspace: str, slug: str, pr_id: int, source: str, destination: str,
                  source_repo: str | None = None, comments: list[dict] | None = None, **extra) -> None:
        full = f"{workspace}/{slug}"
        self.bb_prs[full].append({
            "id": pr_id,
            "title": f"PR {pr_id}",
            "description": f"Description of PR {pr_id}",
            "state": "OPEN",
            "author": {"display_name": "Ada Lovelace"},
            "created_on": "2024-01-02T03:04:05+00:00",
            "source": {"branch": {"name": source}, "repository": {"full_name": source_repo or full}},
            "destination": {"branch": {"name": destination}, "repository": {"full_name": full}},
            "links": {"html": {"href": f"https://bitbucket.org/{full}/pull-requests/{pr_id}"}},
            **extra,
        })
        self.bb_pr_comments[(full, pr_id)] = comments or []

    def add_lfs_object(self, side: str, repo: str, content: bytes) -> str:
        oid = hashlib.sha256(content).hexdigest()
        self.lfs_objects.setdefault(f"{side}/{repo}", {})[oid] = content
        return oid

    def fail(self, method: str, path_pattern: str, status: int, body: dict, headers=None, times: int = 1) -> None:
        self.faults.append(Fault(method, path_pattern, status, body, headers or {}, times))

    def gh_git_path(self, full_name: str) -> Path:
        return self.root / "gh-git" / f"{full_name}.git"

    def calls(self, method: str, pattern: str) -> list[Recorded]:
        return [r for r in self.requests if r.method == method and re.fullmatch(pattern, r.path)]


def _handler(services: FakeServices):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def do_PATCH(self):
            self._dispatch()

        def do_PUT(self):
            self._dispatch()

        # plumbing

        def _read_body(self) -> bytes:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                chunks = []
                while True:
                    size = int(self.rfile.readline().strip(), 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline()
                return b"".join(chunks)
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _auth(self) -> tuple[str, str] | None:
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                user, _, password = base64.b64decode(header[6:]).decode().partition(":")
                return user, password
            if header.startswith(("token ", "Bearer ")):
                return "token", header.split(" ", 1)[1]
            return None

        def _send(self, status: int, payload=None, headers=None, raw: bytes | None = None, content_type="application/json"):
            data = raw if raw is not None else (b"" if payload is None else json.dumps(payload).encode())
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self):
            url = urlsplit(self.path)
            body_bytes = self._read_body()
            is_git = url.path.startswith(("/bb-git/", "/gh-git/"))
            body = None
            if body_bytes and not is_git:
                body = json.loads(body_bytes)
            auth = self._auth()
            with services.lock:
                services.requests.append(Recorded(self.command, url.path, parse_qs(url.query), auth, body))
                for fault in services.faults:
                    if fault.times and fault.method == self.command and re.fullmatch(fault.path_pattern, url.path):
                        fault.times -= 1
                        return self._send(fault.status, fault.body, fault.headers)
            if is_git:
                return self._git(url, body_bytes, auth)
            if url.path.startswith("/bb-api/"):
                if auth != services.bb_auth:
                    return self._send(401, {"type": "error", "error": {"message": "Unauthorized"}})
                return self._bitbucket(url.path[len("/bb-api"):], parse_qs(url.query))
            if url.path.startswith("/gh-api/"):
                if auth != ("token", services.gh_token):
                    return self._send(401, {"message": "Bad credentials"})
                return self._github(url.path[len("/gh-api"):], parse_qs(url.query), body)
            self._send(404, {"message": "not found"})

        # Bitbucket REST

        def _page(self, values: list, query, path: str):
            pagelen = int(query.get("pagelen", ["10"])[0])
            page = int(query.get("page", ["1"])[0])
            start = (page - 1) * pagelen
            data = {"values": values[start:start + pagelen], "page": page, "pagelen": pagelen, "size": len(values)}
            if start + pagelen < len(values):
                params = "&".join(f"{k}={v[0]}" for k, v in query.items() if k != "page")
                data["next"] = f"{services.base}/bb-api{path}?{params}&page={page + 1}"
            return self._send(200, data)

        def _bitbucket(self, path: str, query):
            match = re.fullmatch(r"/repositories/([^/]+)", path)
            if match:
                if not any(name.startswith(match.group(1) + "/") for name in services.bb_repos):
                    return self._send(404, {"error": {"message": "workspace not found"}})
                repos = [r for name, r in sorted(services.bb_repos.items()) if name.startswith(match.group(1) + "/")]
                return self._page(repos, query, path)
            match = re.fullmatch(r"/repositories/([^/]+/[^/]+)", path)
            if match:
                repo = services.bb_repos.get(match.group(1))
                return self._send(200, repo) if repo else self._send(404, {"error": {"message": "not found"}})
            match = re.fullmatch(r"/repositories/([^/]+/[^/]+)/pullrequests", path)
            if match:
                states = query.get("state", ["OPEN"])
                prs = [pr for pr in services.bb_prs[match.group(1)] if pr["state"] in states]
                return self._page(prs, query, path)
            match = re.fullmatch(r"/repositories/([^/]+/[^/]+)/pullrequests/(\d+)/comments", path)
            if match:
                return self._page(services.bb_pr_comments[(match.group(1), int(match.group(2)))], query, path)
            match = re.fullmatch(r"/repositories/([^/]+/[^/]+)/issues", path)
            if match:
                return self._send(410, {"error": {"message": "Issues have been removed"}})
            self._send(404, {"error": {"message": "not found"}})

        # GitHub REST

        def _repo_json(self, full: str) -> dict:
            repo = services.gh_repos[full]
            api = f"{services.base}/gh-api/repos/{full}"
            return {
                "id": abs(hash(full)) % 10000,
                "name": full.split("/")[1],
                "full_name": full,
                "owner": {"login": full.split("/")[0]},
                "private": repo["private"],
                "description": repo["description"],
                "default_branch": repo["default_branch"],
                "url": api,
                "html_url": f"https://github.example/{full}",
            }

        def _item_json(self, full: str, item: dict) -> dict:
            api = f"{services.base}/gh-api/repos/{full}"
            data = {
                "number": item["number"],
                "title": item["title"],
                "body": item["body"],
                "state": item["state"],
                "url": f"{api}/issues/{item['number']}",
                "issue_url": f"{api}/issues/{item['number']}",
                "html_url": f"https://github.example/{full}/pull/{item['number']}",
            }
            if item.get("pull"):
                data["pull_request"] = {"url": f"{api}/pulls/{item['number']}"}
                data.update(head={"ref": item["head"]}, base={"ref": item["base"]}, draft=item["draft"])
            return data

        def _branches(self, full: str) -> list[str]:
            out = subprocess.run(
                ["git", "for-each-ref", "--format=%(refname:strip=2)", "refs/heads"],
                cwd=services.gh_git_path(full), capture_output=True, text=True, check=True,
            ).stdout
            return out.split()

        def _create_repo(self, owner: str, body: dict):
            full = f"{owner}/{body['name']}"
            if full in services.gh_repos:
                return self._send(422, {"message": "Repository creation failed.",
                                        "errors": [{"message": "name already exists on this account"}]})
            services.gh_repos[full] = {
                "private": body.get("private", False),
                "description": body.get("description"),
                "default_branch": "main",
            }
            services.gh_items[full] = []
            path = services.gh_git_path(full)
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "--bare", "--quiet", str(path)], check=True)
            return self._send(201, self._repo_json(full))

        def _github(self, path: str, query, body):
            if path == "/user":
                return self._send(200, {"login": services.gh_login, "id": 1, "url": f"{services.base}/gh-api/user"})
            if path == "/user/repos" and self.command == "POST":
                return self._create_repo(services.gh_login, body)
            match = re.fullmatch(r"/orgs/([^/]+)(/repos)?", path)
            if match:
                if match.group(1) not in services.gh_orgs:
                    return self._send(404, {"message": "Not Found"})
                if match.group(2) and self.command == "POST":
                    return self._create_repo(match.group(1), body)
                return self._send(200, {"login": match.group(1), "id": 2,
                                        "url": f"{services.base}/gh-api/orgs/{match.group(1)}"})
            match = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
            if not match or match.group(1) not in services.gh_repos:
                return self._send(404, {"message": "Not Found"})
            full, rest = match.group(1), match.group(2) or ""
            repo = services.gh_repos[full]
            items = services.gh_items[full]
            if rest == "" and self.command == "GET":
                return self._send(200, self._repo_json(full))
            if rest == "" and self.command == "PATCH":
                if "default_branch" in body:
                    if body["default_branch"] not in self._branches(full):
                        return self._send(422, {"message": "Validation Failed"})
                    repo["default_branch"] = body["default_branch"]
                return self._send(200, self._repo_json(full))
            if rest == "/branches":
                return self._send(200, [{"name": b, "commit": {"sha": "0" * 40}} for b in self._branches(full)])
            if rest == "/pulls" and self.command == "POST":
                branches = self._branches(full)
                if body["head"] not in branches or body["base"] not in branches:
                    return self._send(422, {"message": "Validation Failed", "errors": [{"field": "head"}]})
                item = {"number": len(items) + 1, "title": body["title"], "body": body["body"], "state": "open",
                        "pull": True, "head": body["head"], "base": body["base"], "draft": body.get("draft", False)}
                items.append(item)
                return self._send(201, self._item_json(full, item))
            if rest == "/issues" and self.command == "GET":
                return self._send(200, [self._item_json(full, item) for item in items])
            match = re.fullmatch(r"/issues/(\d+)/comments", rest)
            if match:
                key = (full, int(match.group(1)))
                comments = services.gh_comments.setdefault(key, [])
                if self.command == "POST":
                    comment = {"id": len(comments) + 1, "body": body["body"]}
                    comments.append(comment)
                    return self._send(201, comment)
                return self._send(200, comments)
            self._send(404, {"message": "Not Found"})

        # git smart HTTP and LFS

        def _git(self, url, body: bytes, auth):
            side = url.path.split("/")[1]
            expected = services.bb_git_auth if side == "bb-git" else ("x-access-token", services.gh_token)
            if auth != expected:
                return self._send(401, {"message": "auth required"},
                                  {"WWW-Authenticate": 'Basic realm="fake"'}, content_type="text/plain")
            match = re.fullmatch(rf"/{side}/(.+?)\.git/info/lfs/objects/batch", url.path)
            if match:
                return self._lfs_batch(side, match.group(1), json.loads(body))
            match = re.fullmatch(rf"/{side}/(.+?)\.git/lfs/objects/([0-9a-f]{{64}})", url.path)
            if match:
                store = services.lfs_objects.setdefault(f"{side}/{match.group(1)}", {})
                if self.command == "PUT":
                    store[match.group(2)] = body
                    return self._send(200, raw=b"")
                return self._send(200, raw=store[match.group(2)], content_type="application/octet-stream")
            env = {
                "PATH": os.environ["PATH"],
                "GIT_PROJECT_ROOT": str(services.root / side),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": url.path[len(side) + 1:],
                "QUERY_STRING": url.query,
                "REQUEST_METHOD": self.command,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "REMOTE_USER": auth[0],
                "REMOTE_ADDR": "127.0.0.1",
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            }
            if self.headers.get("Content-Encoding"):
                env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
            result = subprocess.run([str(GIT_HTTP_BACKEND)], input=body, env=env, capture_output=True, check=False)
            head, _, payload = result.stdout.partition(b"\r\n\r\n")
            status, headers = 200, {}
            for line in head.decode().split("\r\n"):
                key, _, value = line.partition(": ")
                if key.lower() == "status":
                    status = int(value.split()[0])
                elif key:
                    headers[key] = value
            content_type = headers.pop("Content-Type", "application/octet-stream")
            self._send(status, raw=payload, headers=headers, content_type=content_type)

        def _lfs_batch(self, side: str, repo: str, request: dict):
            store = services.lfs_objects.setdefault(f"{side}/{repo}", {})
            href = f"{services.base}/{side}/{repo}.git/lfs/objects"
            header = {"Authorization": self.headers["Authorization"]}
            objects = []
            for obj in request["objects"]:
                entry = {"oid": obj["oid"], "size": obj["size"], "authenticated": False}
                if request["operation"] == "download":
                    if obj["oid"] in store:
                        entry["actions"] = {"download": {"href": f"{href}/{obj['oid']}", "header": header}}
                    else:
                        entry["error"] = {"code": 404, "message": "missing"}
                elif obj["oid"] not in store:
                    entry["actions"] = {"upload": {"href": f"{href}/{obj['oid']}", "header": header}}
                objects.append(entry)
            self._send(200, {"transfer": "basic", "objects": objects}, content_type="application/vnd.git-lfs+json")

    return Handler
