"""Load local YAML config and refuse unofficial hosts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from tabbybot.constants import (
    ALLOWED_HOSTS,
    DEFAULT_API_BASE,
    DEFAULT_ORIGIN,
    DEFAULT_REALTIME_URL,
    MAP_NAMES,
    DEFAULT_HUNT_MAP,
)


class ConfigError(Exception):
    pass


def assert_official_url(url: str, *, allow_ws: bool = False) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "wss"):
        raise ConfigError(f"只允許 https/wss 連線：{url}")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ConfigError(f"拒絕非官方主機 {host or '(空)'}，只連線 game.tabby.meme。")
    if parsed.username or parsed.password:
        raise ConfigError("請不要把帳密放在 URL 裡。")
    return url


@dataclass
class HuntConfig:
    map_id: int = DEFAULT_HUNT_MAP
    level_min: int = 1
    level_max: int = 9999
    elite_only: bool = False


@dataclass
class FarmConfig:
    enabled: bool = True
    target_level: int = 30


@dataclass
class MarketConfig:
    interval_sec: int = 8
    qualities: list[str] = field(default_factory=lambda: ["L3", "L4"])
    min_price: float = 0.0
    max_price: float = 200.0
    name_contains: str = ""


@dataclass
class AppConfig:
    api_base: str = DEFAULT_API_BASE
    realtime_url: str = DEFAULT_REALTIME_URL
    origin: str = DEFAULT_ORIGIN
    hunt: HuntConfig = field(default_factory=HuntConfig)
    farm: FarmConfig = field(default_factory=FarmConfig)
    market: MarketConfig = field(default_factory=MarketConfig)

    @property
    def api_origin(self) -> str:
        return self.origin.rstrip("/")


def _as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必須是整數") from exc


def _as_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必須是數字") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ConfigError(f"{name} 無效")
    return number


def _load_mapping(raw: dict[str, Any]) -> AppConfig:
    api_base = str(raw.get("api_base", DEFAULT_API_BASE)).rstrip("/")
    realtime_url = str(raw.get("realtime_url", DEFAULT_REALTIME_URL))
    origin = str(raw.get("origin", DEFAULT_ORIGIN)).rstrip("/")
    assert_official_url(api_base)
    assert_official_url(realtime_url, allow_ws=True)
    assert_official_url(origin)

    hunt_raw = raw.get("hunt") or {}
    if not isinstance(hunt_raw, dict):
        raise ConfigError("hunt 必須是物件")
    map_id = _as_int(hunt_raw.get("map_id", DEFAULT_HUNT_MAP), "hunt.map_id")
    if map_id not in MAP_NAMES:
        raise ConfigError(f"未知地圖編號 {map_id}")
    level_min = _as_int(hunt_raw.get("level_min", 1), "hunt.level_min")
    level_max = _as_int(hunt_raw.get("level_max", 9999), "hunt.level_max")
    if level_min < 1 or level_max < level_min:
        raise ConfigError("怪物等級範圍無效")
    hunt = HuntConfig(
        map_id=map_id,
        level_min=level_min,
        level_max=level_max,
        elite_only=bool(hunt_raw.get("elite_only", False)),
    )

    market_raw = raw.get("market") or {}
    if not isinstance(market_raw, dict):
        raise ConfigError("market 必須是物件")
    qualities = market_raw.get("qualities", ["L3", "L4"])
    if not isinstance(qualities, list) or not qualities:
        raise ConfigError("qualities 必須是非空清單")
    qualities = list(dict.fromkeys(str(item) for item in qualities))
    allowed = {"L1", "L2", "L3", "L4", "*"}
    if any(item not in allowed for item in qualities):
        raise ConfigError("qualities 只能是 L1–L4 或 *")
    if "*" in qualities:
        qualities = ["*"]
    interval = _as_int(market_raw.get("interval_sec", 8), "market.interval_sec")
    if not 5 <= interval <= 3600:
        raise ConfigError("掃描間隔必須在 5–3600 秒之間")
    min_price = _as_float(market_raw.get("min_price", 0), "market.min_price")
    max_price = _as_float(market_raw.get("max_price", 200), "market.max_price")
    if min_price < 0 or max_price <= min_price:
        raise ConfigError("最高價必須大於最低價，且最低價不可為負")
    market = MarketConfig(
        interval_sec=interval,
        qualities=qualities,
        min_price=min_price,
        max_price=max_price,
        name_contains=str(market_raw.get("name_contains", "") or ""),
    )
    farm_raw = raw.get("farm") or {}
    if farm_raw is None:
        farm_raw = {}
    if not isinstance(farm_raw, dict):
        raise ConfigError("farm 必須是物件")
    target_level = _as_int(farm_raw.get("target_level", 30), "farm.target_level")
    if target_level < 1 or target_level > 9999:
        raise ConfigError("練功怪物等級必須在 1–9999")
    farm = FarmConfig(
        enabled=bool(farm_raw.get("enabled", True)),
        target_level=target_level,
    )
    return AppConfig(api_base=api_base, realtime_url=realtime_url, origin=origin, hunt=hunt, farm=farm, market=market)


def config_search_paths() -> list[Path]:
    return [Path.cwd() / "config.yaml", Path.home() / ".tabbybot" / "config.yaml"]


def load_config(path: Path | None = None) -> AppConfig:
    if path is not None:
        candidates = [path]
    else:
        candidates = [item for item in config_search_paths() if item.is_file()]
        if not candidates:
            return _load_mapping({})
    target = candidates[0]
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"無法讀取設定檔 {target}：{exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("設定檔必須是 YAML 物件")
    return _load_mapping(raw)
