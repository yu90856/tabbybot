"""Check GitHub for a newer version and update in place (git pull or zip download).

Never touches private data: settings/wallet live in ~/.tabbybot, and config.yaml,
.venv and .git are skipped when copying a downloaded zip.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO = "yu90856/tabbybot"
BRANCH = "main"
ROOT = Path(__file__).resolve().parent.parent
STAMP = ROOT / ".tabby_version"
SKIP = {".git", ".venv", "config.yaml", ".tabby_version", "__pycache__"}

_cache: dict[str, Any] = {"at": 0.0, "info": None}
_lock = threading.Lock()


def _ctx() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "tabbybot-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as res:
        return res.read()


def _is_git() -> bool:
    return (ROOT / ".git").exists() and shutil.which("git") is not None


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:300])
    return out.stdout.strip()


def local_sha() -> str:
    if _is_git():
        try:
            return _git("rev-parse", "HEAD")
        except Exception:
            pass
    try:
        return STAMP.read_text().strip()
    except OSError:
        return ""


def check(force: bool = False) -> dict[str, Any]:
    """Return {"available", "local", "remote", "message", "date"}; cached for 10 minutes."""
    with _lock:
        if not force and _cache["info"] and time.time() - _cache["at"] < 600:
            return _cache["info"]
    info: dict[str, Any] = {"available": False, "local": local_sha()[:7], "remote": "", "message": "", "date": ""}
    try:
        data = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"))
        remote = data.get("sha", "")
        info["remote"] = remote[:7]
        info["message"] = ((data.get("commit") or {}).get("message") or "").splitlines()[0][:120]
        info["date"] = ((data.get("commit") or {}).get("committer") or {}).get("date", "")
        loc = local_sha()
        if remote and loc != remote:
            info["available"] = True
            if loc and _is_git():
                try:  # local already ahead / contains remote -> not an update
                    _git("fetch", "-q", "origin", BRANCH)
                    if _git("merge-base", "--is-ancestor", remote, "HEAD") == "" :
                        info["available"] = False
                except Exception:
                    pass
    except Exception as exc:  # offline / rate limited: just say nothing
        info["error"] = str(exc)[:200]
    with _lock:
        _cache.update(at=time.time(), info=info)
    return info


def _copy_tree(src: Path, dst: Path) -> None:
    for item in src.iterdir():
        if item.name in SKIP:
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(exist_ok=True)
            _copy_tree(item, target)
        else:
            shutil.copy2(item, target)


def _pip_install() -> None:
    req = ROOT / "requirements.txt"
    if req.exists():
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)], cwd=ROOT, timeout=600)


def apply() -> str:
    """Download and install the latest version. Returns a short status text."""
    before = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
    if _is_git():
        _git("pull", "--ff-only", "origin", BRANCH)
        how = "git"
    else:
        raw = _get(f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}", timeout=120)
        tmp = ROOT / ".update_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            zf.extractall(tmp)
        top = next(p for p in tmp.iterdir() if p.is_dir())
        _copy_tree(top, ROOT)
        shutil.rmtree(tmp, ignore_errors=True)
        remote = check(force=True).get("remote")
        sha = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}")).get("sha", remote or "")
        STAMP.write_text(sha)
        how = "zip"
    after = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
    if after != before:
        _pip_install()
    with _lock:
        _cache.update(at=0.0, info=None)
    log.info("程式已更新（%s）。", how)
    return how


def restart_later(delay: float = 1.5) -> None:
    """Relaunch the same command in a new process, then exit this one."""
    def go() -> None:
        time.sleep(delay)
        args = [sys.executable, "-m", "tabbybot", *sys.argv[1:]]
        # Wait for this process to release the port, then start the new one.
        boot = "import time,subprocess,sys;time.sleep(3);subprocess.call(sys.argv[1:])"
        try:
            subprocess.Popen([sys.executable, "-c", boot, *args], cwd=str(ROOT), close_fds=True)
        finally:
            os._exit(0)
    threading.Thread(target=go, daemon=True).start()
