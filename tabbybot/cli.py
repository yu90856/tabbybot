"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import time
from pathlib import Path

from tabbybot.config import ConfigError, load_config
from tabbybot.constants import MAP_FILES, MAP_NAMES, SPECIAL_ENTRY_MAPS
from tabbybot.loggingutil import configure_logging, short_address
from tabbybot.market import filter_listings, new_matches
from tabbybot.wallet import (
    WalletError,
    address_from_key,
    default_vault_path,
    load_vault,
    normalize_private_key,
    save_vault,
)


def _prompt_key() -> str:
    value = getpass.getpass("請輸入錢包私鑰或助記詞（空格分隔，輸入時不會顯示）：").strip()
    return normalize_private_key(value)


def _prompt_password(confirm: bool = False) -> str:
    password = getpass.getpass("請輸入主密碼：")
    if confirm:
        again = getpass.getpass("請再輸入一次主密碼：")
        if password != again:
            raise WalletError("兩次輸入的主密碼不一致。")
    if len(password) < 8:
        raise WalletError("主密碼至少需要 8 個字元。")
    return password


def resolve_private_key(args: argparse.Namespace) -> str:
    env = os.environ.get("TABBY_PRIVATE_KEY", "").strip()
    if env:
        return normalize_private_key(env)
    vault = Path(args.vault) if getattr(args, "vault", None) else default_vault_path()
    if vault.is_file():
        password = os.environ.get("TABBY_VAULT_PASSWORD") or _prompt_password(confirm=False)
        return load_vault(vault, password).private_key
    return _prompt_key()


def cmd_login(args: argparse.Namespace) -> int:
    key = os.environ.get("TABBY_PRIVATE_KEY", "").strip()
    key = normalize_private_key(key) if key else _prompt_key()
    address = address_from_key(key)
    print(f"錢包地址：{address}")
    if not args.save:
        print("未保存。下次可再用環境變數、保險庫或互動輸入。建議使用專用的空錢包。")
        return 0
    password = os.environ.get("TABBY_VAULT_PASSWORD") or _prompt_password(confirm=True)
    path = Path(args.vault) if args.vault else default_vault_path()
    saved = save_vault(path, key, password)
    print(f"已加密保存到 {path}（{short_address(saved)}）。主密碼不會寫入磁碟。")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    vault = Path(args.vault) if args.vault else default_vault_path()
    hunt_name = MAP_NAMES.get(config.hunt.map_id, str(config.hunt.map_id))
    nav = "有導航資料" if config.hunt.map_id in MAP_FILES else "無導航資料（將直線靠近）"
    special = "（需要特殊道具，v1 不會自動進圖）" if config.hunt.map_id in SPECIAL_ENTRY_MAPS else ""
    print(f"官方 API：{config.api_base}")
    print(f"即時連線：{config.realtime_url}")
    print(f"刷怪地圖：{config.hunt.map_id} {hunt_name} {nav}{special}")
    print(f"等級範圍：{config.hunt.level_min}–{config.hunt.level_max}；只打精英：{config.hunt.elite_only}")
    print(
        f"商城篩選：品質 {','.join(config.market.qualities)}，"
        f"價格 [{config.market.min_price}, {config.market.max_price})，"
        f"間隔 {config.market.interval_sec}s，"
        f"關鍵字 {config.market.name_contains or '（無）'}"
    )
    print(f"保險庫：{'存在 ' + str(vault) if vault.is_file() else '尚未建立'}")
    return 0


def cmd_hunt(args: argparse.Namespace) -> int:
    from tabbybot.hunt import run_hunt

    config = load_config(Path(args.config) if args.config else None)
    key = resolve_private_key(args)
    try:
        return asyncio.run(run_hunt(config, key))
    except KeyboardInterrupt:
        print("\n已停止刷怪。")
        return 0


def cmd_market(args: argparse.Namespace) -> int:
    from tabbybot.client import fetch_shop, signed_login

    config = load_config(Path(args.config) if args.config else None)
    key = resolve_private_key(args)
    token, address, _player = signed_login(key, config)
    print(f"已登入 {short_address(address)}，開始監控商城（不會自動購買）。Ctrl-C 結束。")
    seen: set[tuple[int, float]] = set()
    try:
        while True:
            products = fetch_shop(config, token)
            matches = filter_listings(products, config.market)
            fresh = new_matches(seen, matches)
            seen = {item.dedupe_key for item in matches}
            stamp = time.strftime("%H:%M:%S")
            if fresh:
                print(f"[{stamp}] 新命中 {len(fresh)} 件（目前符合 {len(matches)} / 掃描 {len(products)}）")
                for item in fresh:
                    label = item.name or f"#{item.product_id}"
                    print(f"  - {label}  品質 {item.quality}  價格 {item.price}  id={item.product_id}")
            else:
                print(f"[{stamp}] 掃描 {len(products)} 件，符合 {len(matches)} 件，無新命中")
            time.sleep(config.market.interval_sec)
    except KeyboardInterrupt:
        print("\n已停止商城監控。")
        return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from tabbybot.gui import run_gui

    config = load_config(Path(args.config) if args.config else None)
    vault = Path(args.vault) if args.vault else default_vault_path()
    return run_gui(
        config,
        vault_path=vault,
        port=args.port,
        open_browser=not args.no_browser,
    )


def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        prog="tabbybot",
        description="維洛傳說（Tabby）非官方單帳號工具：自動巡邏打怪與商城監控。只連官方伺服器，不會自動購買或自動更新。",
    )
    parser.add_argument("-c", "--config", help="設定檔路徑（預設為 ./config.yaml 或 ~/.tabbybot/config.yaml）")
    parser.add_argument("--vault", help="加密保險庫路徑（預設 ~/.tabbybot/wallet.vault）")
    parser.add_argument("-v", "--verbose", action="store_true", help="顯示除錯日誌")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="輸入私鑰或助記詞；可選擇用主密碼加密保存")
    login.add_argument("--save", action="store_true", help="以主密碼加密寫入本機保險庫")
    login.set_defaults(func=cmd_login)

    status = sub.add_parser("status", help="顯示目前設定（不含任何機密）")
    status.set_defaults(func=cmd_status)

    hunt = sub.add_parser("hunt", help="在設定的地圖上自動巡邏並打怪")
    hunt.set_defaults(func=cmd_hunt)

    market = sub.add_parser("market", help="監控商城符合條件的上架（不會購買）")
    market.set_defaults(func=cmd_market)

    gui = sub.add_parser("gui", help="開啟本機網頁介面（僅限這台電腦）")
    gui.add_argument("--port", type=int, default=8765, help="本機埠號，預設 8765；0 表示隨機")
    gui.add_argument("--no-browser", action="store_true", help="不要自動開啟瀏覽器")
    gui.set_defaults(func=cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)
    try:
        return int(args.func(args) or 0)
    except (WalletError, ConfigError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"錯誤：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
