"""
RemoteSync Client Library
A clean, consolidated API for D-Link DIR-850L RemoteSync Web File Access.
"""

from __future__ import annotations

import logging
import random
import time
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Generator
from urllib.parse import quote

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logger = logging.getLogger("remote_sync")


# ======================================================================
# Exceptions
# ======================================================================
class RemoteSyncError(Exception):
    """Base class for all RemoteSync exceptions."""
    pass

class RemoteSyncConnectionError(RemoteSyncError):
    pass

class RemoteSyncLoginError(RemoteSyncError):
    pass


# ======================================================================
# HTTP API Client
# ======================================================================
@dataclass
class RemoteFile:
    name: str
    path: str
    is_directory: bool
    size: Optional[int] = None
    mtime: Optional[int] = None
    extension: str = ""


class RemoteSyncAPIClient:
    """Direct HTTP client for the RemoteSync AJAX API."""

    def __init__(
        self,
        base_url: str,
        user_id: str,
        tok: str,
        volid: str,
        cookies: dict[str, str],
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.tok = tok
        self.volid = volid
        self.timeout = timeout

        self._session = requests.Session()
        for k, v in cookies.items():
            self._session.cookies.set(k, v)
        
        # Ensure correct Accept header as seen in browser
        self._session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        })

    def list_volumes(self) -> list[dict]:
        return self._api_json(f"ListRoot?id={self.user_id}&tok={self.tok}").get("item", [])

    def list_directory(self, path: str) -> list[RemoteFile]:
        encoded_path = quote(path, safe="")
        url = (
            f"ListFile?id={self.user_id}&tok={self.tok}"
            f"&volid={self.volid}&path={encoded_path}&random={random.random()}"
        )
        data = self._api_json(url)
        
        results = []
        for obj in data.get("files", []):
            is_dir = bool(obj.get("type") == "folder")
            results.append(RemoteFile(
                name=obj.get("name", ""),
                path=f"{path.rstrip('/')}/{obj.get('name', '')}",
                is_directory=is_dir,
                size=int(obj.get("size", 0)) if not is_dir else None,
                mtime=int(obj.get("mtime", 0)),
                extension=str(obj.get("desp", "")).upper(),
            ))
        return results

    def _walk(self, current_path: str, depth: int, max_depth: int) -> Generator[RemoteFile, None, None]:
        if depth > max_depth:
            return
        
        items = self.list_directory(current_path)
        for item in items:
            yield item
            if item.is_directory:
                yield from self._walk(item.path, depth + 1, max_depth)

    def iter_all_files(self, root_path: str, max_depth: int = 64) -> Generator[RemoteFile, None, None]:
        yield from self._walk(root_path, depth=0, max_depth=max_depth)

    def download_file_to_disk(self, path: str, filename: str, dest: Path, chunk_size: int = 65536) -> Path:
        url = (
            f"{self.base_url}/dws/api/GetFile"
            f"?id={self.user_id}&tok={self.tok}&volid={self.volid}"
            f"&path={quote(path, safe='')}&filename={quote(filename, safe='')}"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            resp = self._session.get(url, timeout=self.timeout, stream=True)
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if chunk:
                        fh.write(chunk)
            tmp.replace(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return dest

    def create_folder(self, path: str, dirname: str) -> None:
        encoded_path = quote(path, safe="")
        encoded_dirname = quote(dirname, safe="")
        data = self._api_json(
            f"AddDir?id={self.user_id}&tok={self.tok}"
            f"&volid={self.volid}&path={encoded_path}&dirname={encoded_dirname}"
        )
        if data.get("status") != "ok":
            logger.warning("create_folder %s/%s returned %s", path, dirname, data)

    def upload_file(self, path: str, filename: str, local_file: Path) -> None:
        url = f"{self.base_url}/dws/api/UploadFile?{random.random()}"
        data = {
            "id": self.user_id,
            "tok": self.tok,
            "volid": self.volid,
            "path": path + "/",
            "filename": filename,
        }
        with local_file.open("rb") as f:
            files = {"file": (filename, f, "application/octet-stream")}
            resp = self._session.post(url, data=data, files=files, timeout=self.timeout)
            resp.raise_for_status()

    def _api_json(self, query: str) -> dict:
        url = f"{self.base_url}/dws/api/{query}"
        resp = self._session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {}


# ======================================================================
# Sync Engine
# ======================================================================
@dataclass
class SyncStats:
    downloaded: int = 0
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    dirs_created: int = 0
    bytes_transferred: int = 0
    elapsed_seconds: float = 0.0

    def report(self) -> str:
        mb = self.bytes_transferred / (1024 * 1024)
        return (
            f"Sync complete in {self.elapsed_seconds:.1f}s — "
            f"{self.downloaded} downloaded, {self.uploaded} uploaded ({mb:.1f} MB), "
            f"{self.skipped} skipped, {self.failed} failed, {self.dirs_created} dirs created"
        )


class FolderSyncEngine:
    def __init__(
        self,
        api: RemoteSyncAPIClient,
        remote_root: str,
        local_root: Path,
        *,
        direction: str = "pull",
        dry_run: bool = False,
    ) -> None:
        self.api = api
        self.remote_root = remote_root.rstrip("/")
        self.local_root = Path(local_root).resolve()
        self.direction = direction.lower()
        self.dry_run = dry_run

        if self.direction not in ("pull", "push", "two_way"):
            raise ValueError(f"Invalid sync direction: {self.direction}")

        if not dry_run:
            self.local_root.mkdir(parents=True, exist_ok=True)

    def _local_path(self, remote_path: str) -> Path:
        rel = remote_path
        if rel.startswith(self.remote_root):
            rel = rel[len(self.remote_root):]
        rel = rel.lstrip("/")
        return self.local_root / Path(rel)

    def sync(self) -> SyncStats:
        stats = SyncStats()
        t0 = time.monotonic()

        logger.info(
            "Syncing %s <-> %s (direction=%s, dry_run=%s)",
            self.remote_root, self.local_root, self.direction, self.dry_run,
        )

        remote_tree: dict[str, RemoteFile] = {}
        logger.info("Scanning remote directory tree...")
        for entry in self.api.iter_all_files(self.remote_root):
            remote_tree[entry.path] = entry

        local_tree: dict[str, Path] = {}
        logger.info("Scanning local directory tree...")
        if self.local_root.exists():
            for p in self.local_root.rglob("*"):
                rel = p.relative_to(self.local_root)
                equiv_remote_path = f"{self.remote_root}/{rel.as_posix()}"
                local_tree[equiv_remote_path] = p

        actions: list[tuple[str, str, Optional[RemoteFile], Optional[Path]]] = []
        all_paths = set(remote_tree.keys()) | set(local_tree.keys())
        
        for path in sorted(all_paths):
            remote_entry = remote_tree.get(path)
            local_entry = local_tree.get(path)

            is_dir = False
            if remote_entry and remote_entry.is_directory:
                is_dir = True
            elif local_entry and local_entry.is_dir():
                is_dir = True
                
            if is_dir:
                if not local_entry and self.direction in ("pull", "two_way"):
                    actions.append(("MKDIR_LOCAL", path, remote_entry, None))
                if not remote_entry and self.direction in ("push", "two_way"):
                    actions.append(("MKDIR_REMOTE", path, None, local_entry))
                continue
                
            if remote_entry and not local_entry:
                if self.direction in ("pull", "two_way"):
                    actions.append(("DOWNLOAD", path, remote_entry, self._local_path(path)))
            elif local_entry and not remote_entry:
                if self.direction in ("push", "two_way"):
                    actions.append(("UPLOAD", path, None, local_entry))
            elif remote_entry and local_entry:
                local_mtime = int(local_entry.stat().st_mtime)
                remote_mtime = remote_entry.mtime
                if remote_mtime > local_mtime + 2 and self.direction in ("pull", "two_way"):
                    actions.append(("DOWNLOAD", path, remote_entry, local_entry))
                elif local_mtime > remote_mtime + 2 and self.direction in ("push", "two_way"):
                    actions.append(("UPLOAD", path, remote_entry, local_entry))
                else:
                    stats.skipped += 1

        for action, path, remote_entry, local_entry in actions:
            if action == "MKDIR_LOCAL":
                dest = self._local_path(path)
                if not self.dry_run:
                    dest.mkdir(parents=True, exist_ok=True)
                    stats.dirs_created += 1
            
            elif action == "MKDIR_REMOTE":
                parent_path = path[:path.rfind("/")]
                dirname = path[path.rfind("/")+1:]
                if not self.dry_run:
                    try:
                        self.api.create_folder(parent_path, dirname)
                        stats.dirs_created += 1
                    except Exception as exc:
                        logger.error("FAIL MKDIR_REMOTE %s: %s", path, exc)
                        stats.failed += 1

            elif action == "DOWNLOAD":
                logger.info("DOWNLOAD %s", path)
                if not self.dry_run:
                    folder_path = path[:path.rfind("/")]
                    try:
                        self.api.download_file_to_disk(
                            path=folder_path,
                            filename=remote_entry.name,
                            dest=local_entry,
                        )
                        if remote_entry.mtime:
                            os.utime(local_entry, (remote_entry.mtime, remote_entry.mtime))
                        stats.downloaded += 1
                        stats.bytes_transferred += local_entry.stat().st_size
                    except Exception as exc:
                        logger.error("FAIL DOWNLOAD %s: %s", path, exc)
                        stats.failed += 1
                else:
                    stats.downloaded += 1

            elif action == "UPLOAD":
                logger.info("UPLOAD   %s", path)
                if not self.dry_run:
                    folder_path = path[:path.rfind("/")]
                    filename = path[path.rfind("/")+1:]
                    try:
                        self.api.upload_file(
                            path=folder_path,
                            filename=filename,
                            local_file=local_entry,
                        )
                        stats.uploaded += 1
                        stats.bytes_transferred += local_entry.stat().st_size
                    except Exception as exc:
                        logger.error("FAIL UPLOAD %s: %s", path, exc)
                        stats.failed += 1
                else:
                    stats.uploaded += 1

        stats.elapsed_seconds = time.monotonic() - t0
        logger.info(stats.report())
        return stats


# ======================================================================
# Selenium Auth Client
# ======================================================================
class RemoteSyncClient:
    """Selenium client to negotiate the MD5 auth challenge and acquire tokens."""

    def __init__(self, base_url: str, username: str, password: str, download_dir: str, headless: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self.headless = headless
        self.driver: Optional[webdriver.Chrome] = None
        self._logged_in = False

    def start(self) -> None:
        logger.info("Starting RemoteSync client (Selenium)")
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--window-size=1400,1000")
        options.add_argument("--disable-notifications")
        
        try:
            self.driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            raise RemoteSyncConnectionError(f"Failed to launch Chrome WebDriver: {exc}") from exc

        self.driver.set_page_load_timeout(20)
        logger.info("Connecting to %s", self.base_url)
        try:
            self.driver.get(self.base_url)
        except WebDriverException as exc:
            raise RemoteSyncConnectionError(f"Could not reach RemoteSync at {self.base_url}: {exc}") from exc

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            finally:
                self.driver = None
        logger.info("RemoteSync client closed")

    def login(self) -> None:
        if self.driver is None:
            raise RemoteSyncConnectionError("start() must be called before login()")

        logger.info("Attempting login as user '%s'", self._username)
        try:
            wait = WebDriverWait(self.driver, 15)
            username_field = wait.until(EC.presence_of_element_located((By.ID, "user_name")))
            password_field = self.driver.find_element(By.ID, "user_pwd")
            submit_button = self.driver.find_element(By.ID, "login")
        except TimeoutException as exc:
            raise RemoteSyncLoginError("Could not locate username/password fields on login page") from exc

        username_field.clear()
        username_field.send_keys(self._username)
        password_field.clear()
        password_field.send_keys(self._password)
        
        login_url = self.driver.current_url
        submit_button.click()

        try:
            wait.until(EC.url_changes(login_url))
        except TimeoutException:
            pass

        if self.driver.current_url != login_url:
            self._logged_in = True
            logger.info("Login successful (redirected to %s)", self.driver.current_url)
            return
            
        raise RemoteSyncLoginError("Login failed: URL did not change.")

    def get_api_session(self) -> tuple[str, str, str, dict]:
        if not self._logged_in or self.driver is None:
            raise RemoteSyncLoginError("Must be logged in before calling get_api_session()")

        folder_view_url = f"{self.base_url}/folder_view.php"
        if "folder_view.php" not in self.driver.current_url:
            logger.info("Navigating to folder_view.php to acquire session token")
            self.driver.get(folder_view_url)

        _js_check = "return (typeof storage_user !== 'undefined' && storage_user.get('tok')) || null;"
        wait = WebDriverWait(self.driver, 15)
        try:
            wait.until(lambda d: d.execute_script(_js_check))
        except TimeoutException:
            raise RemoteSyncConnectionError("folder_view.php JS runtime did not initialise in time.")

        user_id = self.driver.execute_script("return storage_user.get('id') || '';")
        tok     = self.driver.execute_script("return storage_user.get('tok') || '';")
        volid   = self.driver.execute_script("return storage_user.get('volid') || '';")

        if not volid:
            try:
                el = self.driver.find_element(By.XPATH, "//a[contains(@href,'folder_view.php#') and string-length(text()) > 0]")
                volid = el.text.strip()
            except Exception:
                pass

        cookies = {c["name"]: c["value"] for c in self.driver.get_cookies()}
        logger.info("API session acquired: id=%r volid=%r tok=<redacted>", user_id, volid)
        return user_id, tok, volid, cookies
