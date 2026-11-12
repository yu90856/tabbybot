"""Official-server HTTP login and WebSocket session. Hosts are allow-listed."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import websockets
from eth_account import Account
from eth_account.messages import encode_defunct

from tabbybot.config import AppConfig, assert_official_url
from tabbybot.quests import extract_quests, is_quest_limit_response, quest_error_text
from tabbybot.constants import CHAIN_ID, PING_INTERVAL_SEC, PONG_TIMEOUT_SEC
from tabbybot.loggingutil import redact_mapping, short_address, token_fingerprint
from tabbybot.protocol import ProtocolError, attach_seq_life, build_auth_frame, build_ping
from tabbybot.wallet import normalize_private_key

log = logging.getLogger(__name__)


class ClientError(Exception):
    pass


class LoginError(ClientError):
    pass


def challenge_retry_delay(payload: dict | None, attempt: int, retry_after_header: str | None = None) -> float:
    candidates: list[tuple[object, bool]] = []
    if retry_after_header:
        candidates.append((retry_after_header, False))
    if isinstance(payload, dict):
        for key in ("retryAfterMs", "retry_after_ms"):
            if payload.get(key) is not None:
                candidates.append((payload[key], True))
        for key in ("retryAfter", "retry_after", "retryAfterSeconds"):
            if payload.get(key) is not None:
                candidates.append((payload[key], False))
    delay = None
    for value, milliseconds in candidates:
        try:
            delay = float(value) / (1000.0 if milliseconds else 1.0)
            break
        except (TypeError, ValueError):
            if milliseconds:
                continue
            try:
                retry_at = parsedate_to_datetime(str(value))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                break
            except (TypeError, ValueError, OverflowError):
                continue
    if delay is None:
        delay = min(2 ** max(0, int(attempt)), 8)
    return min(max(delay, 0.5), 15.0)


def api_root_from_realtime(realtime_url: str, api_base: str) -> str:
    assert_official_url(realtime_url, allow_ws=True)
    assert_official_url(api_base)
    parsed = urlsplit(realtime_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/realtime"):
        path = path[: -len("/realtime")]
    if path:
        scheme = "https" if parsed.scheme == "wss" else "http"
        return urlunsplit((scheme, parsed.netloc, path, "", "")).rstrip("/")
    return api_base.rstrip("/")


def _json_request(
    url: str,
    *,
    method: str,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    assert_official_url(url)
    raw = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=raw, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            header_map = {k.lower(): v for k, v in response.headers.items()}
            if not isinstance(payload, dict):
                raise LoginError("伺服器回傳不是 JSON 物件")
            return response.status, payload, header_map
    except urllib.error.HTTPError as exc:
        raw_text = exc.read().decode("utf-8", "replace")
        header_map = {k.lower(): v for k, v in exc.headers.items()}
        try:
            payload = json.loads(raw_text) if raw_text else {}
        except json.JSONDecodeError:
            payload = {"message": raw_text[:180]}
        if not isinstance(payload, dict):
            payload = {"message": str(payload)[:180]}
        payload.setdefault("_http_status", exc.code)
        return exc.code, payload, header_map


def signed_login(private_key: str, config: AppConfig) -> tuple[str, str, dict[str, Any]]:
    """EIP-191 login against official membership endpoints. Returns (token, address, player)."""
    key = normalize_private_key(private_key)
    account = Account.from_key(key)
    api = config.api_base.rstrip("/")
    origin = config.api_origin
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": origin + "/",
    }

    def post(path: str, body: dict, bearer: str | None = None, retries: int = 1) -> dict[str, Any]:
        url = api + path
        req_headers = dict(headers)
        if bearer:
            req_headers["Authorization"] = "Bearer " + bearer
        last_payload: dict[str, Any] = {}
        attempts = retries
        for attempt in range(attempts):
            status, payload, resp_headers = _json_request(url, method="POST", body=body, headers=req_headers)
            last_payload = payload
            code = str(payload.get("code", "")).lower()
            if path == "/membership/challenge" and (status == 200 and code == "try_again" or status in (429, 502, 503, 504) or (code == "try_again" and status >= 400)):
                if attempt + 1 < attempts:
                    delay = challenge_retry_delay(payload if isinstance(payload, dict) else None, attempt, resp_headers.get("retry-after"))
                    log.info("登入挑戰暫時無法取得，%.1f 秒後重試（%s/%s）", delay, attempt + 1, attempts - 1)
                    time.sleep(delay)
                    continue
                raise LoginError("伺服器連續多次暫時無法產生簽名挑戰，請稍後再試。")
            if status >= 400:
                detail = payload.get("code") or payload.get("message") or f"HTTP {status}"
                raise LoginError(f"{path}: {detail}")
            if payload.get("code") and not payload.get("walletToken") and not payload.get("token"):
                raise LoginError(str(payload["code"]))
            return payload
        raise LoginError(f"{path}: 未取得有效回應")

    challenge = post("/membership/challenge", {"address": account.address, "chainId": CHAIN_ID}, retries=5)
    challenge_id = challenge.get("challengeId")
    message = challenge.get("message")
    if not challenge_id or not isinstance(message, str) or not message:
        raise LoginError("簽名挑戰缺少 challengeId 或 message")
    signed = Account.sign_message(encode_defunct(text=message), key)
    signature = "0x" + bytes(signed.signature).hex()
    verification = post("/membership/verify", {"challengeId": challenge_id, "signature": signature, "chainId": CHAIN_ID})
    wallet_token = verification.get("walletToken")
    if not wallet_token:
        raise LoginError("簽名驗證未回傳 walletToken")
    entered = post("/membership/enter", {}, str(wallet_token))
    token = entered.get("token")
    if not token:
        raise LoginError("入場驗證未回傳遊戲會話；請確認這個錢包已有測試服資格。")
    player = fetch_player(config, str(token))
    log.info("簽名登入成功：%s；%s", short_address(account.address), token_fingerprint(str(token)))
    return str(token), account.address, player


def fetch_player(config: AppConfig, token: str) -> dict[str, Any]:
    url = config.api_base.rstrip("/") + "/player"
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    status, payload, _ = _json_request(url, method="GET", headers=headers, timeout=10)
    if status == 404 or str(payload.get("code", "")).lower() in {"no_character", "character_not_found", "not_found"}:
        raise LoginError("這個錢包還沒有角色。請先用官方客戶端 https://game.tabby.meme/ 建立角色後再回來。")
    if status >= 400:
        raise LoginError(payload.get("message") or payload.get("code") or f"讀取角色資料失敗 HTTP {status}")
    return payload


def claim_defeat(config: AppConfig, token: str, encounter_id: str) -> dict[str, Any] | None:
    url = config.api_base.rstrip("/") + "/progression/defeat"
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        status, payload, _ = _json_request(url, method="POST", body={"encounterId": encounter_id}, headers=headers, timeout=8)
    except Exception as exc:
        log.warning("領取擊敗獎勵失敗：%s", type(exc).__name__)
        return None
    if status >= 400:
        log.warning("領取擊敗獎勵被拒：%s", payload.get("code") or payload.get("message") or status)
        return None
    return payload


class QuestError(ClientError):
    pass


class QuestLimitReached(QuestError):
    pass


def fetch_quests(config: AppConfig, token: str) -> list[dict[str, Any]]:
    url = config.api_base.rstrip("/") + "/progression/quests"
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": config.api_origin,
        "Referer": config.api_origin + "/",
    }
    status, payload, _ = _json_request(url, method="GET", headers=headers, timeout=10)
    if status >= 400:
        raise QuestError(payload.get("message") or payload.get("code") or f"讀取任務失敗 HTTP {status}")
    return extract_quests(payload)


def accept_quest(config: AppConfig, token: str, quest_id: int) -> dict[str, Any]:
    url = config.api_base.rstrip("/") + "/progression/quests/accept"
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": config.api_origin,
        "Referer": config.api_origin + "/",
    }
    status, payload, _ = _json_request(
        url,
        method="POST",
        body={"questId": int(quest_id)},
        headers=headers,
        timeout=10,
    )
    if is_quest_limit_response(payload) or status == 429:
        raise QuestLimitReached(quest_error_text(payload) or "今日任務領取已滿")
    if status >= 400:
        raise QuestError(payload.get("message") or payload.get("code") or f"接取任務失敗 HTTP {status}")
    return payload


def _quest_headers(config: AppConfig, token: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": config.api_origin,
        "Referer": config.api_origin + "/",
    }


def cancel_quest(config: AppConfig, token: str, quest_id: int) -> dict[str, Any]:
    """Cancel an accepted quest (game rule: still uses one of today's quest slots)."""
    url = config.api_base.rstrip("/") + "/progression/quests/cancel"
    status, payload, _ = _json_request(url, method="POST", body={"questId": int(quest_id)}, headers=_quest_headers(config, token), timeout=10)
    if status >= 400:
        raise QuestError(payload.get("message") or payload.get("code") or f"取消任務失敗 HTTP {status}")
    return payload


def fetch_quest_delivery(config: AppConfig, token: str, quest_id: int) -> dict[str, Any]:
    url = config.api_base.rstrip("/") + f"/quests/delivery/{int(quest_id)}"
    status, payload, _ = _json_request(url, method="GET", headers=_quest_headers(config, token), timeout=10)
    if status == 404:  # endpoint lives under /progression like accept
        url = config.api_base.rstrip("/") + f"/progression/quests/delivery/{int(quest_id)}"
        status, payload, _ = _json_request(url, method="GET", headers=_quest_headers(config, token), timeout=10)
    if status >= 400:
        raise QuestError(payload.get("message") or payload.get("code") or f"讀取交付物品失敗 HTTP {status}")
    return payload


def deliver_quest(
    config: AppConfig,
    token: str,
    quest_id: int,
    run_number: int,
    item_ids: list[int],
) -> dict[str, Any]:
    body = {"questId": int(quest_id), "runNumber": int(run_number), "itemIds": [int(item) for item in item_ids]}
    status = 404
    for path in ("/quests/deliver", "/progression/quests/deliver"):
        url = config.api_base.rstrip("/") + path
        status, payload, _ = _json_request(url, method="POST", body=body, headers=_quest_headers(config, token), timeout=15)
        if status != 404:
            break
    if status >= 400:
        raise QuestError(payload.get("message") or payload.get("code") or f"交付任務失敗 HTTP {status}")
    if not (isinstance(payload, dict) and payload.get("delivered")):
        raise QuestError(payload.get("message") or payload.get("code") or "伺服器未確認交付成功")
    return payload


def fetch_shop(config: AppConfig, token: str) -> list[dict[str, Any]]:
    url = config.api_base.rstrip("/") + "/shop"
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": config.api_origin,
        "Referer": config.api_origin + "/",
    }
    status, payload, _ = _json_request(url, method="GET", headers=headers, timeout=25)
    if status >= 400:
        raise ClientError(payload.get("message") or payload.get("code") or f"商城請求失敗 HTTP {status}")
    products = payload.get("products", [])
    if not isinstance(products, list):
        raise ClientError("商城回應沒有商品列表")
    return [item for item in products if isinstance(item, dict)]


def player_has_character(player: dict[str, Any]) -> bool:
    if not player:
        return False
    if player.get("needsCharacter") or player.get("noCharacter"):
        return False
    ident = player.get("id") or player.get("playerId") or player.get("playerName") or player.get("name")
    return bool(ident)


def player_class(player: dict[str, Any], fallback: str = "WARRIOR") -> str:
    value = player.get("characterClass") or player.get("class") or fallback
    return str(value).upper()


PacketHandler = Callable[[dict[str, Any]], None]


@dataclass
class RealtimeSession:
    config: AppConfig
    token: str
    on_packet: PacketHandler
    ws: Any = None
    sequence: int = 0
    life: int = 0
    last_pong_at: float = 0.0
    closed: bool = False
    close_reason: str = ""
    _sender_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def connect(self) -> None:
        assert_official_url(self.config.realtime_url, allow_ws=True)
        url = self.config.realtime_url
        self.ws = await websockets.connect(
            url,
            additional_headers={"Origin": self.config.origin},
            open_timeout=20,
            max_size=2**22,
        )
        self.sequence = 0
        self.life = 0
        self.last_pong_at = time.monotonic()
        self.closed = False
        await self.ws.send(json.dumps(build_auth_frame(self.token), separators=(",", ":")))
        log.info("已連上即時伺服器並送出登入幀。")

    async def send_game(self, packet: dict[str, Any]) -> None:
        if self.ws is None:
            raise ClientError("尚未連線")
        async with self._sender_lock:
            self.sequence += 1
            payload = attach_seq_life(packet, self.sequence, self.life)
            try:
                await self.ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            except Exception:
                self.sequence -= 1
                raise

    async def send_ping(self) -> None:
        await self.send_game(build_ping())

    def note_pong(self) -> None:
        self.last_pong_at = time.monotonic()

    def pong_is_stale(self) -> bool:
        return time.monotonic() - self.last_pong_at > PONG_TIMEOUT_SEC

    async def recv_loop(self) -> None:
        assert self.ws is not None
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    continue
                try:
                    packet = json.loads(message)
                except json.JSONDecodeError:
                    log.debug("略過非 JSON 幀")
                    continue
                if not isinstance(packet, dict):
                    continue
                if packet.get("type") == "pong":
                    self.note_pong()
                kind = packet.get("type")
                if kind not in {"snapshot", "pong"}:
                    log.debug("RECV %s", redact_mapping({k: packet[k] for k in list(packet)[:8]}))
                try:
                    self.on_packet(packet)
                except Exception:
                    # A handler bug must not tear down the WebSocket (that looked like a mysterious re-login).
                    log.exception("處理伺服器封包時發生錯誤（%s）", packet.get("type") or packet.get("code"))
        except websockets.ConnectionClosed as exc:
            reason = ""
            try:
                rcvd = getattr(exc, "rcvd", None)
                reason = str(getattr(rcvd, "reason", "") or "")
            except Exception:
                reason = ""
            self.close_reason = reason or str(exc)
            log.info("WebSocket 已關閉。")
        finally:
            self.closed = True

    async def close(self) -> None:
        self.closed = True
        ws, self.ws = self.ws, None
        if ws is not None:
            await ws.close()


async def ping_watchdog(session: RealtimeSession, stop: asyncio.Event) -> None:
    while not stop.is_set() and not session.closed:
        try:
            await asyncio.wait_for(stop.wait(), timeout=PING_INTERVAL_SEC)
            return
        except asyncio.TimeoutError:
            pass
        if session.closed or session.ws is None:
            return
        if session.pong_is_stale() and session.last_pong_at:
            log.warning("超過 %s 秒沒有收到 pong，準備中斷。", PONG_TIMEOUT_SEC)
            await session.close()
            return
        try:
            await session.send_ping()
        except Exception as exc:
            log.warning("心跳送出失敗：%s", type(exc).__name__)
            await session.close()
            return



def _authorized_json(
    config: AppConfig,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    token: str,
    timeout: int = 10,
) -> tuple[int, dict[str, Any]]:
    url = config.api_base.rstrip("/") + path
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Origin": config.api_origin,
        "Referer": config.api_origin + "/",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    status, payload, _ = _json_request(url, method=method, body=body, headers=headers, timeout=timeout)
    return status, payload


def fetch_equipment(config: AppConfig, token: str) -> dict[str, Any]:
    status, payload = _authorized_json(config, "/equipment", token=token)
    if status >= 400:
        raise ClientError(payload.get("message") or payload.get("code") or f"讀取裝備失敗 HTTP {status}")
    return payload


def fetch_progression(config: AppConfig, token: str) -> dict[str, Any]:
    status, payload = _authorized_json(config, "/progression", token=token)
    if status >= 400:
        raise ClientError(payload.get("message") or payload.get("code") or f"讀取進度失敗 HTTP {status}")
    return payload


def equip_item(config: AppConfig, token: str, player_item_id: object) -> dict[str, Any]:
    """POST /equipment/equip with a playerItemId. User-initiated only."""
    status, payload = _authorized_json(
        config,
        "/equipment/equip",
        method="POST",
        body={"playerItemId": player_item_id},
        token=token,
    )
    if status >= 400:
        raise ClientError(payload.get("message") or payload.get("code") or f"穿戴失敗 HTTP {status}")
    return payload


def unequip_item(config: AppConfig, token: str, slot_type: str) -> dict[str, Any]:
    """POST /equipment/unequip with a slotType. User-initiated only."""
    status, payload = _authorized_json(
        config,
        "/equipment/unequip",
        method="POST",
        body={"slotType": slot_type},
        token=token,
    )
    if status >= 400:
        raise ClientError(payload.get("message") or payload.get("code") or f"卸下失敗 HTTP {status}")
    return payload
