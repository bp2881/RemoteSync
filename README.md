# Remote Sync

A blazing-fast, hybrid synchronization tool designed specifically for the **D-Link DIR-850L RemoteSync Web File Access**. 

This engine bridges the gap between Windows and Linux machines by using your router's USB drive as a central synchronization hub. No SMB? No CIFS? No problem! We bypass the slow Web UI by cracking open the router's undocumented internal API to deliver lightning-fast, bi-directional folder syncing.

---

## How It Works

The D-Link RemoteSync interface poses a massive challenge: it protects itself using a complex, Javascript-driven **HMAC-MD5 challenge-response** login. You can't just throw a simple HTTP request at it.

To solve this, our engine uses a two-phase architecture:

1. **Phase 1: The Heist (Selenium)**
   We briefly launch a headless instance of Google Chrome. Selenium navigates to the router, solves the MD5 login challenge, and securely extracts the golden session token (`tok`) directly from the Javascript runtime. Once the token is acquired, the browser is immediately terminated.
   
2. **Phase 2: The Sync (Direct HTTP API)**
   Armed with the session token, our custom Python engine hooks directly into the router's undocumented REST API (`/dws/api/`). It recursively scans the remote folder tree and compares it to your local filesystem. Using raw HTTP `GET` and `POST` requests, it flawlessly uploads and downloads files based on modification timestamps.

---

## Features

- **Bi-Directional Sync:** Push local changes, pull remote changes, or use `two_way` to keep both sides perfectly mirrored based on file modification times.
- **Lightning Fast:** File transfers happen via direct HTTP streams, avoiding the overhead of UI automation.
- **Additive-Only Safety:** By design, the engine will overwrite older files but will **never delete** a file. Your data is safe.
- **Cross-Platform:** Works seamlessly on both Windows and Arch Linux, acting as a bridge between the two.

---

## Setup & Usage

1. Create a virtual environment and install the dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install requests selenium
   ```

2. Copy the environment template and add your credentials:
   ```bash
   cp .env.example .env
   # Edit .env with your router IP, username, password, and sync paths
   ```

3. Configure your sync direction in `.env`:
   - `REMOTE_SYNC_SYNC_DIRECTION=pull` (Router ➔ Local)
   - `REMOTE_SYNC_SYNC_DIRECTION=push` (Local ➔ Router)
   - `REMOTE_SYNC_SYNC_DIRECTION=two_way` (Newest file wins)

4. Run the sync engine:
   ```bash
   python main.py
   ```

---

## Limitations

- **Fat32/NTFS Timestamp Resolution:** The router's filesystem handling means file modification timestamps (mtimes) can sometimes be slightly imprecise. We use a 2-second buffer window to prevent infinite re-sync loops.
- **No Deletions:** To protect against catastrophic data loss during two-way sync, deletion propagation is currently disabled. If you delete a file locally, it will not be deleted on the router.
- **Browser Dependency:** Even though the sync happens via HTTP, Google Chrome (or Chromium) must be installed on the host machine to execute the initial HMAC-MD5 login challenge via Selenium.

---

## Future Scope

- **Reverse-Engineering the MD5 Challenge:** Completely removing the Selenium dependency by porting the router's Javascript HMAC-MD5 cryptography directly into Python, enabling the script to run on headless micro-servers (like a Raspberry Pi) without Chrome.
- **Continuous Daemon Mode:** Implementing a filesystem watcher (like `watchdog`) to automatically push file changes to the router in real-time, rather than requiring a manual script execution.
- **Deletion Propagation:** Safely adding support for an `--allow-deletes` flag using a local state file to track whether a file was intentionally deleted vs simply missing.
- **Conflict Resolution:** Handling scenarios where a file is modified simultaneously on both Windows and Linux before a sync occurs.
