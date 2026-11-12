"""Measured hunting efficiency per (map, level band). Only real observations, no estimates."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tabbybot.constants import MAP_NAMES

MIN_SAMPLES = 5
EXP_ATTRIBUTION_WINDOW_SEC = 30.0


def level_band(level: Any) -> int | None:
    try:
        value = int(level)
    except (TypeError, ValueError):
        return None
    if value < 1:
        return None
    return max(10, (value // 10) * 10)


def extract_experience(payload: Any) -> tuple[int, int] | None:
    """Return (level, experience) from a progression/profile/self payload if present."""
    if not isinstance(payload, dict):
        return None
    for source in (payload, payload.get("progression"), payload.get("progress"), payload.get("player")):
        if not isinstance(source, dict):
            continue
        exp = source.get("experience", source.get("exp"))
        if exp is None:
            continue
        try:
            level = int(source.get("level", 0) or 0)
            return level, int(exp)
        except (TypeError, ValueError):
            continue
    return None


CYCLE_MAX_SEC = 180.0  # longer gaps (pauses, travel) fall back to fight time only


def _empty() -> dict[str, Any]:
    return {"kills": 0, "fight_secs": 0.0, "timed_kills": 0, "exp": 0, "exp_kills": 0, "potions": 0, "aborts": 0,
            "cycle_secs": 0.0, "cycle_kills": 0}


class EfficiencyTracker:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.rows: dict[str, dict[str, Any]] = {}
        self.travel_secs = 0.0
        self.idle_secs = 0.0
        self.since = time.strftime("%Y-%m-%d %H:%M:%S")
        self._dirty = False
        self._saved_at = 0.0
        self._last_exp: tuple[int, int] | None = None
        self._pending: list[tuple[float, str]] = []  # kills awaiting an EXP update
        self.load()

    @staticmethod
    def key(map_id: Any, band: int) -> str:
        return f"{int(map_id)}:{int(band)}"

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for key, row in (data.get("rows") or {}).items():
            if isinstance(row, dict):
                merged = _empty()
                merged.update({k: row[k] for k in merged if k in row})
                self.rows[str(key)] = merged
        self.travel_secs = float(data.get("travel_secs", 0) or 0)
        self.idle_secs = float(data.get("idle_secs", 0) or 0)
        self.since = str(data.get("since") or self.since)

    def save(self, now: float, *, force: bool = False, interval: float = 30.0) -> None:
        if self.path is None or not self._dirty or (not force and now - self._saved_at < interval):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"since": self.since, "rows": self.rows, "travel_secs": round(self.travel_secs, 1),
                                       "idle_secs": round(self.idle_secs, 1)}, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self.path)
            self._dirty = False
            self._saved_at = now
        except OSError:
            pass

    def reset(self) -> None:
        self.rows = {}
        self.travel_secs = 0.0
        self.idle_secs = 0.0
        self.since = time.strftime("%Y-%m-%d %H:%M:%S")
        self._pending = []
        self._dirty = True
        self.save(0.0, force=True)

    def _row(self, map_id: Any, band: int) -> dict[str, Any]:
        return self.rows.setdefault(self.key(map_id, band), _empty())

    # ---- recording ----------------------------------------------------------
    def record_kill(self, map_id: Any, level: Any, fight_secs: float | None, potions: int, now: float,
                    cycle_secs: float | None = None) -> None:
        """cycle_secs: time since the previous kill (walking, kiting, waiting for respawn included)."""
        band = level_band(level)
        if band is None or map_id is None:
            return
        row = self._row(map_id, band)
        row["kills"] += 1
        if cycle_secs is not None and 0 < cycle_secs <= CYCLE_MAX_SEC:
            row["cycle_secs"] += float(cycle_secs)
            row["cycle_kills"] += 1
        if fight_secs is not None and 0 < fight_secs < 600:
            row["fight_secs"] += float(fight_secs)
            row["timed_kills"] += 1
        row["potions"] += max(0, int(potions))
        self._pending.append((now, self.key(map_id, band)))
        self._dirty = True

    def record_abort(self, map_id: Any, level: Any) -> None:
        band = level_band(level)
        if band is None or map_id is None:
            return
        self._row(map_id, band)["aborts"] += 1
        self._dirty = True

    def add_time(self, kind: str, secs: float) -> None:
        if secs <= 0 or secs > 5:
            return
        if kind in {"travel", "portal", "explore"}:
            self.travel_secs += secs
        elif kind in {"idle", "wait", "patrol"}:
            self.idle_secs += secs

    def observe_experience(self, level: int, experience: int, now: float) -> None:
        """Attribute an EXP increase to the kills since the last reading, only when
        all of them share the same (map, band) so the per-kill value is exact."""
        previous = self._last_exp
        self._last_exp = (level, experience)
        pending = [item for item in self._pending if now - item[0] <= EXP_ATTRIBUTION_WINDOW_SEC]
        if previous is None:
            self._pending = []
            return
        if (level, experience) == previous:
            self._pending = pending
            return
        self._pending = []
        if level != previous[0] or experience < previous[1] or not pending:
            return  # level-up resets EXP, or no kill to attribute: skip, never estimate
        keys = {key for _t, key in pending}
        if len(keys) != 1:
            return
        row = self.rows.get(keys.pop())
        if row is None:
            return
        row["exp"] += experience - previous[1]
        row["exp_kills"] += len(pending)
        self._dirty = True

    # ---- reporting ----------------------------------------------------------
    def table(self) -> dict[str, Any]:
        rows = []
        for key, row in self.rows.items():
            try:
                map_id, band = (int(part) for part in key.split(":"))
            except ValueError:
                continue
            avg_exp = row["exp"] / row["exp_kills"] if row["exp_kills"] else None
            fight_avg = row["fight_secs"] / row["timed_kills"] if row["timed_kills"] else None
            # Time per kill = whole cycle (incl. walking / waiting for respawn) when measured.
            avg_secs = row["cycle_secs"] / row["cycle_kills"] if row.get("cycle_kills") else fight_avg
            per_min = (avg_exp / avg_secs * 60.0) if avg_exp is not None and avg_secs else None
            rows.append({
                "map_id": map_id,
                "map_name": MAP_NAMES.get(map_id, str(map_id)),
                "band": band,
                "label": f"Lv{band}",
                "kills": row["kills"],
                "avg_exp": round(avg_exp, 1) if avg_exp is not None else None,
                "avg_secs": round(avg_secs, 1) if avg_secs is not None else None,
                "fight_secs": round(fight_avg, 1) if fight_avg is not None else None,
                "exp_per_min": round(per_min, 1) if per_min is not None else None,
                "potions_per_kill": round(row["potions"] / row["kills"], 2) if row["kills"] else None,
                "aborts": row["aborts"],
                "enough": row["kills"] >= MIN_SAMPLES,
                "best": False,
            })
        rows.sort(key=lambda r: (r["exp_per_min"] is None, -(r["exp_per_min"] or 0), r["band"]))
        ranked = [r for r in rows if r["enough"] and r["exp_per_min"] is not None]
        if ranked:
            ranked[0]["best"] = True
        return {"rows": rows, "since": self.since, "travel_secs": round(self.travel_secs),
                "idle_secs": round(self.idle_secs), "min_samples": MIN_SAMPLES}
