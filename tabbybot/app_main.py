"""Entry point for the packaged desktop app (Mac .app / Windows .exe).

Starts the local web console and opens it in the browser. Private data
(wallet, settings) always lives in ~/.tabbybot and is never bundled.
If port 8765 is already in use, a free port is chosen so a running
instance is not disturbed.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import traceback
from pathlib import Path

PREFERRED_PORT = 8765


def _user_dir() -> Path:
    path = Path.home() / ".tabbybot"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _setup_logging() -> Path:
    log_path = _user_dir() / "app.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
        force=True,
    )
    return log_path


def _port_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _choose_port(preferred: int = PREFERRED_PORT) -> int:
    if _port_available(preferred):
        return preferred
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _parse_port_arg(argv: list[str]) -> int | None:
    args = list(argv)
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                return None
    return None


def main(argv: list[str] | None = None) -> int:
    try:
        log_path = _setup_logging()
        try:
            os.chdir(_user_dir())
        except OSError:
            pass
        os.environ.setdefault("PYTHONUTF8", "1")
        from tabbybot.cli import main as cli_main

        argv = list(sys.argv[1:] if argv is None else argv)
        port = _parse_port_arg(argv)
        if port is None:
            port = _choose_port()
        cmd = ["gui", "--port", str(port)]
        if "--no-browser" in argv:
            cmd.append("--no-browser")
        logging.getLogger(__name__).info("啟動控制台，埠號 %s（日誌 %s）", port, log_path)
        return int(cli_main(cmd) or 0)
    except Exception:
        try:
            crash = _user_dir() / "app.log"
            with crash.open("a", encoding="utf-8") as fh:
                fh.write("\n啟動失敗：\n" + traceback.format_exc())
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
