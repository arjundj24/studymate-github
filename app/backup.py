"""Off-host persistence for free hosts with an ephemeral disk.

Most no-card free tiers (Hugging Face Spaces, Render) throw the filesystem away
when the container restarts or wakes up. This module keeps a compressed snapshot
of ``data/`` in a *private Hugging Face dataset repo* (free, no credit card) and
restores it on boot, so your library survives redeploys and cold starts.

Design notes
------------
* Everything is optional: with no token it is a no-op and the app behaves exactly
  as it does locally.
* Page renders and region crops are *excluded* from the snapshot — they are
  regenerated on demand, and including them would turn a 2 MB archive into 60 MB.
* Pushes are debounced and happen on a daemon thread, so a slow upload never
  blocks a request.
* The snapshot is only pulled when the local data directory is empty, so a
  machine with real local data is never overwritten by a stale remote copy.
"""
from __future__ import annotations

import io
import os
import tarfile
import threading
import time
from pathlib import Path

import config as CFG

ARCHIVE = "studymate-data.tar.gz"
SKIP_DIRS = {"__pycache__"}
# generated images: rebuilt on demand by the renderer
SKIP_SUFFIX = (".png",)

_lock = threading.Lock()
_last_hash = None
_pending = False


def enabled() -> bool:
    if not (getattr(CFG, "BACKUP_ENABLED", False) and CFG.BACKUP_REPO):
        return False
    try:                                  # the SDK is optional locally
        import huggingface_hub  # noqa: F401,PLC0415
        return True
    except Exception:                     # noqa: BLE001
        print("[backup] huggingface_hub not installed — mirroring disabled", flush=True)
        return False


def _api():
    """Lazy import so the app runs without huggingface_hub installed."""
    from huggingface_hub import HfApi  # noqa: PLC0415

    return HfApi(token=CFG.BACKUP_TOKEN)


def _local_signature() -> str:
    """Cheap change detector: newest mtime + total file count under data/."""
    newest, count = 0.0, 0
    root = Path(CFG.DATA_DIR)
    if not root.exists():
        return "0:0"
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            try:
                st = os.stat(os.path.join(dirpath, f))
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            count += 1
    return f"{newest:.0f}:{count}"


def snapshot_bytes() -> bytes:
    """tar.gz of data/ without the regenerable images."""
    buf = io.BytesIO()
    root = Path(CFG.DATA_DIR)
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if name.endswith(SKIP_SUFFIX):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                try:
                    tar.add(full, arcname=rel)
                except OSError:
                    continue
    return buf.getvalue()


def push(force: bool = False) -> bool:
    """Upload a snapshot if anything changed. Returns True when it uploaded."""
    global _last_hash
    if not enabled():
        return False
    sig = _local_signature()
    with _lock:
        if not force and sig == _last_hash:
            return False
        try:
            data = snapshot_bytes()
            api = _api()
            api.create_repo(CFG.BACKUP_REPO, repo_type="dataset", private=True, exist_ok=True)
            api.upload_file(
                path_or_fileobj=io.BytesIO(data),
                path_in_repo=ARCHIVE,
                repo_id=CFG.BACKUP_REPO,
                repo_type="dataset",
                commit_message=f"snapshot {time.strftime('%Y-%m-%d %H:%M')}",
            )
            _last_hash = sig
            print(f"[backup] pushed {len(data) / 1024:.0f} KB to {CFG.BACKUP_REPO}", flush=True)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[backup] push failed: {type(e).__name__}: {e}", flush=True)
            return False


def restore_on_boot() -> bool:
    """Download the last snapshot when this machine has no data yet."""
    if not enabled():
        return False
    local = Path(CFG.DATA_DIR)
    has_data = local.exists() and any(local.iterdir())
    if has_data:
        print("[backup] local data present — not restoring", flush=True)
        return False
    try:
        from huggingface_hub import hf_hub_download  # noqa: PLC0415

        path = hf_hub_download(CFG.BACKUP_REPO, ARCHIVE, repo_type="dataset", token=CFG.BACKUP_TOKEN)
        local.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall(local)  # noqa: S202  (our own archive, our own repo)
        print(f"[backup] restored library from {CFG.BACKUP_REPO}", flush=True)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[backup] nothing to restore ({type(e).__name__}): {e}", flush=True)
        return False


def _watcher() -> None:
    global _pending
    quiet_for = 0
    while True:
        time.sleep(5)
        try:
            sig = _local_signature()
            if sig != _last_hash:
                quiet_for += 5
                if quiet_for >= CFG.BACKUP_SECONDS:
                    if push():
                        quiet_for = 0
            else:
                quiet_for = 0
        except Exception:  # noqa: BLE001
            time.sleep(20)


def start_watcher() -> None:
    if not enabled():
        return
    global _last_hash
    _last_hash = _local_signature()
    t = threading.Thread(target=_watcher, name="studymate-backup", daemon=True)
    t.start()
    print(f"[backup] mirroring data/ to {CFG.BACKUP_REPO} every ~{CFG.BACKUP_SECONDS}s of quiet", flush=True)
