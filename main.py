"""
main_sync.py — RemoteSync folder-sync entry point.

Workflow
--------
1. Selenium login (HMAC-MD5 challenge-response, same as main.py)
2. Navigate to folder_view.php → extract tok + cookies from JS runtime
3. Close browser (Selenium no longer needed)
4. Use RemoteSyncAPIClient (pure HTTP) to walk the remote folder tree
5. FolderSyncEngine downloads new / updated files to the local sync dir

Configuration (via .env)
------------------------
  REMOTE_SYNC_URL                  Router base URL
  REMOTE_SYNC_USERNAME             Login username
  REMOTE_SYNC_PASSWORD             Login password
  REMOTE_SYNC_VOLUME_ID            USB volume ID  (e.g. SanDisk_SANDISK_72541)
  REMOTE_SYNC_SYNC_REMOTE_PATH     Remote folder to sync  (e.g. /SanDisk_SANDISK_72541/Pranav)
  REMOTE_SYNC_SYNC_LOCAL_PATH      Local destination directory
  REMOTE_SYNC_SYNC_DRY_RUN         Set to "true" to log without downloading
"""

import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env manually (avoids dotenv.find_dotenv assertion in pipe contexts)
# ---------------------------------------------------------------------------
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("remote_sync")

# ---------------------------------------------------------------------------
# Imports (after env is loaded)
# ---------------------------------------------------------------------------
from remote_sync import RemoteSyncAPIClient
from remote_sync import RemoteSyncClient
from remote_sync import FolderSyncEngine


def main() -> None:
    base_url    = os.environ["REMOTE_SYNC_URL"]
    username    = os.environ["REMOTE_SYNC_USERNAME"]
    password    = os.environ["REMOTE_SYNC_PASSWORD"]
    volume_id   = os.environ.get("REMOTE_SYNC_VOLUME_ID", "")
    remote_path = os.environ.get(
        "REMOTE_SYNC_SYNC_REMOTE_PATH",
        f"/{volume_id}" if volume_id else "",
    )
    local_path  = os.environ.get(
        "REMOTE_SYNC_SYNC_LOCAL_PATH",
        str(Path("sync") / (remote_path.strip("/").replace("/", "_") or "remote_sync")),
    )
    direction   = os.environ.get("REMOTE_SYNC_SYNC_DIRECTION", "pull").lower()
    dry_run     = os.environ.get("REMOTE_SYNC_SYNC_DRY_RUN", "").lower() in ("1", "true", "yes")

    if not remote_path:
        logger.error(
            "REMOTE_SYNC_SYNC_REMOTE_PATH is not set. "
            "Set it in .env, e.g.  REMOTE_SYNC_SYNC_REMOTE_PATH=/SanDisk_SANDISK_72541/Pranav"
        )
        sys.exit(1)

    logger.info("Remote path : %s", remote_path)
    logger.info("Local path  : %s", Path(local_path).resolve())
    logger.info("Direction   : %s", direction)
    logger.info("Dry run     : %s", dry_run)

    # ------------------------------------------------------------------
    # Step 1: Selenium login + token extraction
    # ------------------------------------------------------------------
    client = RemoteSyncClient(
        base_url=base_url,
        username=username,
        password=password,
        download_dir="downloads",
        headless=True,   # headless for sync — no window needed
    )

    try:
        client.start()
        client.login()
        user_id, tok, volid, cookies = client.get_api_session()
    finally:
        client.close()

    # Prefer env-configured volid; fall back to what JS reported.
    if volume_id:
        volid = volume_id

    if not tok:
        logger.error("Failed to acquire tok from folder_view.php. Cannot proceed.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 2: Build direct HTTP client
    # ------------------------------------------------------------------
    api = RemoteSyncAPIClient(
        base_url=base_url,
        user_id=user_id or username,
        tok=tok,
        volid=volid,
        cookies=cookies,
    )

    # Quick connectivity check: list volumes
    try:
        vols = api.list_volumes()
        logger.info("Volumes on device: %s", [v.get("volid") for v in vols])
    except Exception as exc:
        logger.warning("Could not list volumes (continuing anyway): %s", exc)

    # ------------------------------------------------------------------
    # Step 3: Sync
    # ------------------------------------------------------------------
    engine = FolderSyncEngine(
        api=api,
        remote_root=remote_path,
        local_root=Path(local_path),
        direction=direction,
        dry_run=dry_run,
    )
    stats = engine.sync()
    print()
    print(stats.report())


if __name__ == "__main__":
    main()
