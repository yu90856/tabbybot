# Tabby / 維洛傳說 official protocol notes (from public client + open reference study)

Official hosts only:
- https://game.tabby.meme
- wss://game.tabby.meme/api/realtime
- Origin / Referer for HTTP membership: https://game.tabby.meme
- Chain ID: 56 (BSC)

## Login (HTTP, POST JSON)

API base is derived from the WS URL by stripping a trailing `/realtime`.
Default API base: `https://game.tabby.meme/api`

Headers for membership endpoints (browser-origin validation required):
- Accept: application/json
- Content-Type: application/json
- Origin: https://game.tabby.meme
- Referer: https://game.tabby.meme/

1. POST `/membership/challenge` body `{"address": "<checksum address>", "chainId": 56}`
   - Success: `{"challengeId": "...", "message": "..."}`
   - Transient: HTTP 200 or error with `code: "try_again"` — safe to retry (idempotent). Also retry HTTP 429/502/503/504. Bound delay 0.5–15s; honor retryAfterMs / Retry-After; else exponential backoff min(2^attempt, 8).
   - Do NOT blindly retry `/membership/verify` or `/membership/enter` (one-time).
2. Sign `message` locally with EIP-191 personal_sign: `Account.sign_message(encode_defunct(text=message), key)`.
   Signature must be RPC-style `0x` + 65-byte hex.
3. POST `/membership/verify` body `{"challengeId", "signature", "chainId": 56}`
   - Success includes `walletToken`.
4. POST `/membership/enter` body `{}` with `Authorization: Bearer <walletToken>`
   - Success includes game session `token` (JWT; may contain playerId, exp).
5. GET `/player` with `Authorization: Bearer <game token>`
   - If no character, user must create one in the official game first (v1 does not create characters). Surface a clear error.

Never log the private key, walletToken, game token, or signature. Log only address prefix/suffix and token SHA256-12 fingerprint.

## WebSocket

URL: `wss://game.tabby.meme/api/realtime`
Handshake: Origin `https://game.tabby.meme`. Official client does NOT send Authorization bearer on the handshake (default off).
First text frame after connect (auth packet). Official client reorders so `token` is inserted before `loginReset` and `snapshotDelta`:

```json
{"action":"auth","movementRevision":0,"map":1,"x":544.0,"y":352.0,"animation":"idle","attackCritical":false,"flip":false,"token":"<game token>","loginReset":true,"snapshotDelta":2}
```

map/x/y here are a default spawn hint; the server `welcome` position is authoritative.

Every subsequent client game packet is JSON text with `seq` (monotonically increasing, starting at 1 after connect) and `life` (integer from latest snapshot `self.life`, starts 0 until known) added:

```json
{"action":"...","seq":1,"life":0, ...}
```

Keepalive: `{"action":"ping","sentAt": <monotonic_ms>}` about every few seconds. If no `type=pong` for 12s, reconnect.
Server packets use `type` (not `action`) for kind: welcome, snapshot, pong, error, travel_result, attack_ready, hit, ...

### welcome
`{"type":"welcome","position":{"map":2,"x":...,"y":...,"revision":N}}`
Adopt map, x, y, movement_revision from this. Position is authoritative.

### snapshot
`{"type":"snapshot","action":"snapshot"|"respawn"|..., "delta":bool, "deltaVersion": 0|2, "self":{...}, "monsters":[...], "removedMonsters":[...], "rewards":[{"EncounterID":"..."}], "position":{...}, "peers":[...], "progress":{...}, "serverTime":...}`

- Non-delta: replace monster cache. Delta: merge. If `deltaVersion != 2`, also prune ids not in this frame's `monsters` list (legacy).
- `removedMonsters`: drop those encounterIds.
- Monster rows have `encounterId`, `mapId`, `x`, `y`, `health`, optionally name/level/rank.
- `self.life` updates `life` used on outgoing packets.
- `self.nextAttack` is an attack ticket `{id, critical?}`.
- `self.dead` true => stop combat, send `{"action":"respawn"}`, wait for snapshot action=respawn with position.
- `self` on deltas is partial; merge into last_self_state. Full snapshot (delta false) replaces.
- First snapshot marks world ready: send a stationary idle sync, start ping + periodic idle sync when not moving (~1s+).
- rewards with EncounterID: POST `https://game.tabby.meme/api/progression/defeat` JSON `{"encounterId"}` with Bearer game token (claim loot).

### attack_ready
`{"type":"attack_ready","nextAttack":{"id":"...","critical":false}}`

### error codes of interest
- login_required: do not retry the same token blindly
- movement_too_fast / invalid_position / combat_locked: if packet.position present and its revision is not older than local, snap to server x/y/map/revision; pause and retry path. Ignore stale (older revision) corrections.
- monsterAttack errors: invalid_target does not consume the ticket (un-mark used). attack_cooldown: keep ticket, wait ~0.62s; may include nextAttack.

### travel (needed if character is not already on the hunt map)
Adjacent maps only. Routes:
1↔2, 1↔4, 1↔5, 1↔6, 2↔10, 3↔6, 4↔9, 6↔8, 7↔1.
Special maps 5 (ruins, ticket) and 7 (treasure stone) are out of v1 scope if they need items.
Packet: `{"action":"travel","map":DEST,"questId":0,"useTicket":false,"movementRevision":R}`
Wait for `type=travel_result`. If ok, adopt x,y,revision and map=DEST. Must be at a portal in the official client, but server validates; if travel fails, print reason and do not loop forever.

## Movement sync
`{"action":"sync","movementRevision":R,"map":M,"x":X,"y":Y,"animation":"idle"|"run"|"attack","attackCritical":bool,"flip":bool}`
Optional when attacking: `attackElevation` (-1, 0, or 1).

movementRevision is SERVER-authored. Client sends the last known revision; it does not increment it locally on each step. Server may correct position.

Movement speed used by the reference for stepping: 105 * 1.15 * 1.15 * 1.2. Patrol steps are conservative: min(dist, speed*dt*0.4, 7.0). Chase: min(dist, speed*dt, speed/10). Tick ~66ms. Round x/y to 2 decimals.

## Combat
Reach: warrior/guard 55 (impact 60); archer 295 (impact 310). Stop chase around 45 / 285.
To attack:
1. Have unused ticket id and not in cooldown.
2. Within reach.
3. Send sync animation=attack, attackCritical from ticket, flip if monster.x < player.x, attackElevation from dy vs dx.
4. Mark ticket used. After impact delay, send `{"action":"monsterAttack","encounterId":"...","attackTicket":"...","queuedAt": <monotonic_ms>}`.
5. After animation end, send idle sync.
Impact timing (published skins): frame_duration = (0.88/12) / speed_multiplier; speed_multiplier = 1.2 * clamp(1+attackSpeedPercent/100, 1, 2); warrior impact frame 5; archer frame 7 (or 5 if attacking downward elevation==1). animation 12 frames. busy until max(0.62/speed_mult, anim) + 0.06.

If ticket missing, wait. Prefer nearest living monster on current map (health>0). Optional filters later: level, elite, uncontested — v1 can include simple level min/max from config and skip dead/wrong map.

If no targets: patrol using sweep waypoints from navigation data (one walkable point per 320px sector, snake order). Pathfind to next sector. If a monster appears, cancel patrol and chase.

## Pathfinding (reimplement; do not copy source)
Navigation JSON files in data/maps/: `{cell_size: 64, cells: [[cx,cy],...], obstacles: [{name, rx, ry, x, y}, ...]}`.
Walkable if floor(x/64),floor(y/64) is in cells, and not inside any obstacle ellipse expanded by player clearance 16: abs(dx)<rx' and abs(dy)<ry' and (dx/rx')^2+(dy/ry')^2 < 1. Bin obstacles by 128px for speed.
If line is clear (sample every 4px), path is just [goal]. Else A* on a grid (step 16 in town map 1, 32 elsewhere), 8-connected, snap start/goal to nearest walkable grid within radius 5, then string-pull / skip waypoints when line is clear. Return waypoints excluding start, including goal. Empty list if impossible.
Sweep path: sector key (cx//5, cy//5); pick candidate closest to sector center; skip return-portal neighborhood for maps 8/9/10 around (3232, 6240) radius 220; order by (sy, sx * (-1 if sy%2 else 1)).

Map file ids: 1 town_navigation.json, 2 snowfield, 7 treasure, 8 cave, 9 oasis, 10 frozen. Maps 3,4,5,6 have no navigation JSON — if selected, fall back to direct-line approach and say so.

## Market
GET `https://game.tabby.meme/api/shop` with:
- Authorization: Bearer <game token>
- Accept / Content-Type json
- Origin/Referer official site (Referer https://game.tabby.meme/)
Response object with `products` list. Fields used for filtering: id, price, qualityCode (L1-L4), state=="ACTIVE", market is not False, reserved is falsy.
Filter (v1, NO purchase): quality in configured list or `*`; minPrice <= price < maxPrice; optional name/substring or item type if present on the product (be defensive about missing fields).
Poll interval from config, default >= 5s. Print matches. No POST /order. No auto-buy.

## Character classes
WARRIOR / GUARD / ARCHER (and maybe LANCER). Read from GET /player or snapshot self.characterClass.
