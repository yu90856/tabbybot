"""Check GitHub Releases for a newer version.

The web console shows a banner when a newer release exists. Choosing
「更新」opens the release download page; updates are never forced.
Private data (settings/wallet) lives in ~/.tabbybot and is never touched.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tabbybot import __version__

log = logging.getLogger(__name__)

REPO = "yu90856/tabbybot"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
ROOT = Path(__file__).resolve().parent.parent

_cache: dict[str, Any] = {"at": 0.0, "info": None}
_lock = threading.Lock()


def _ctx() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "tabbybot-updater",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as res:
        return res.read()


def _version_tuple(value: str) -> tuple[int, int, int]:
    text = value.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    parts: list[int] = []
    for chunk in text.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits or 0))
        if len(parts) == 3:
            break
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def check(force: bool = False) -> dict[str, Any]:
    """Return release info; cached for 10 minutes.

    Keys: available, local, remote, message, date, url.
    """
    with _lock:
        if not force and _cache["info"] and time.time() - _cache["at"] < 600:
            return _cache["info"]
    local = str(__version__)
    info: dict[str, Any] = {
        "available": False,
        "local": local,
        "remote": "",
        "message": "",
        "date": "",
        "url": RELEASES_PAGE,
    }
    try:
        data = json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/latest"))
        tag = str(data.get("tag_name") or "")
        remote = tag[1:] if tag[:1] in ("v", "V") else tag
        info["remote"] = remote or tag
        name = str(data.get("name") or "").strip()
        body_line = ""
        body = data.get("body")
        if isinstance(body, str) and body.strip():
            body_line = body.strip().splitlines()[0].strip()
        info["message"] = (name or body_line)[:120]
        info["date"] = str(data.get("published_at") or "")
        info["url"] = str(data.get("html_url") or RELEASES_PAGE)
        if remote and _version_tuple(remote) > _version_tuple(local):
            info["available"] = True
    except urllib.error.HTTPError as exc:
        # 404: no releases published yet — not an error for the user.
        if exc.code != 404:
            info["error"] = str(exc)[:200]
    except Exception as exc:  # offline / rate limited: just say nothing
        info["error"] = str(exc)[:200]
    with _lock:
        _cache.update(at=time.time(), info=info)
    return info


def apply() -> str:
    """Kept for compatibility; packaged apps do not auto-install.

    Opens nothing and does not rewrite files. Callers should send the
    user to the URL from check() instead.
    """
    if getattr(sys, "frozen", False):
        raise RuntimeError("應用程式版本請到 GitHub Releases 下載新檔，不會自動覆蓋。")
    raise RuntimeError("請到 GitHub Releases 下載新版本。不會自動覆蓋本機檔案。")


def restart_later(delay: float = 1.5) -> None:
    """Relaunch the same command in a new process, then exit this one."""

    def go() -> None:
        time.sleep(delay)
        args = [sys.executable, "-m", "tabbybot", *sys.argv[1:]]
        boot = "import time,subprocess,sys;time.sleep(3);subprocess.call(sys.argv[1:])"
        try:
            subprocess.Popen([sys.executable, "-c", boot, *args], cwd=str(ROOT), close_fds=True)
        finally:
            os._exit(0)

    threading.Thread(target=go, daemon=True).start()
