(function () {
  const $ = (id) => document.getElementById(id);
  const toast = (text) => {
    const el = $("toast");
    el.textContent = text;
    el.hidden = !text;
    if (text) setTimeout(() => { if (el.textContent === text) el.hidden = true; }, 4000);
  };
  const showError = (id, err) => {
    const el = $(id);
    el.hidden = !err;
    el.textContent = err || "";
  };

  async function api(path, body) {
    const options = { method: "GET", headers: { "Accept": "application/json" } };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const res = await fetch(path, options);
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok || data.ok === false) throw new Error(data.error || "操作失敗");
    return data;
  }

  let selectedMap = 9;
  let lastState = null;

  function renderMaps(state) {
    const box = $("map-grid");
    const here = state.hunt && state.hunt.map_id;
    box.innerHTML = "";
    (state.maps || []).forEach((item) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "map-btn";
      if (item.id === selectedMap) btn.classList.add("is-on");
      if (item.id === here) btn.classList.add("is-here");
      btn.textContent = item.id + "　" + item.label;
      btn.title = item.title + (item.special ? "（需要特殊道具）" : "");
      btn.addEventListener("click", async () => {
        selectedMap = item.id;
        try {
          await api("/api/hunt/options", { map_id: item.id });
          if (state.connected && !item.special) {
            const result = await api("/api/hunt/travel", { map_id: item.id });
            if (result.message) toast(result.message);
          } else if (item.special) {
            toast("這張地圖需要特殊道具，不會從這裡進入。");
          }
        } catch (err) { toast(err.message); }
        refresh();
      });
      box.appendChild(btn);
    });
  }

  function renderLogs(state) {
    const lines = (state.logs || []).map((row) => (row.time ? row.time + "  " : "") + row.text);
    $("log").textContent = lines.join("\n") || "還沒有紀錄。";
    $("log").scrollTop = $("log").scrollHeight;
  }


  function badgeClass(row) {
    if (row.skip && row.state !== "已完成" && row.state !== "進行中") return "skip";
    if (row.state === "已完成") return "done";
    if (row.state === "進行中") return "active";
    if (row.state === "可接取") return "ready";
    return "locked";
  }

  function renderDelivery(state) {
    window.__lastState = state;
    const modal = $("delivery-modal");
    if (!modal) return;
    const prompt = state.pending_delivery;
    if (!prompt || (window.__deliveryClosed != null && window.__deliveryClosed === prompt.quest_id)) {
      modal.hidden = true;
      return;
    }
    modal.hidden = false;
    $("delivery-title").textContent = "確認交付「" + (prompt.title || "任務") + "」";
    $("delivery-quest").textContent = (prompt.map_name && prompt.map_name !== "—" ? prompt.map_name + " · " : "") +
      "需要 " + (prompt.target || "?") + " 件";
    const items = prompt.proposed || [];
    const list = $("delivery-items");
    if (!items.length) {
      list.innerHTML = "<li>尚未列出確切物品。確認後會先查背包再選最低品質的候選；數量不足時不會提交。</li>";
    } else {
      list.innerHTML = items.map((item) => "<li>" + escapeHtml(item.label || item.name || "物品") +
        (item.quantity && item.quantity > 1 ? "（數量 " + item.quantity + "）" : "") + "</li>").join("");
    }
    $("delivery-count").textContent = items.length
      ? "將消耗以下 " + items.length + " 件物品或裝備，確認後才會交給伺服器。"
      : "還不能列出將消耗的確切物品。";
    $("delivery-note").textContent = prompt.note || prompt.error || "";
    $("btn-delivery-confirm").disabled = !!(prompt.previewed && items.length && !prompt.enough);
  }

  function renderQuests(state) {
    const status = $("quest-status");
    const table = $("quest-table");
    if (!status || !table) return;
    status.textContent = state.quest_error || state.quest_status || "尚未讀取任務";
    const rows = state.quests || [];
    const body = table.querySelector("tbody");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="4">還沒有任務資料。解鎖後會自動讀取，或按「重新整理任務」。</td></tr>';
      return;
    }
    body.innerHTML = rows.map((row) => {
      const label = row.skip && row.state !== "已完成" ? row.skip : row.state;
      return "<tr>" +
        "<td>" + escapeHtml(row.title || "—") + "</td>" +
        "<td>" + escapeHtml(row.map_name || "—") + "</td>" +
        "<td>" + escapeHtml(row.progress_text || "—") + "</td>" +
        "<td><span class=\"badge " + badgeClass(row) + "\">" + escapeHtml(label || "—") + "</span></td>" +
        "</tr>";
    }).join("");
  }

  function renderMarket(state) {
    $("market-summary").textContent = (state.market && state.market.summary) || "";
    const body = $("market-table").querySelector("tbody");
    body.innerHTML = "";
    const rows = (state.market && state.market.matches) || [];
    if (!rows.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td colspan='4'>目前沒有符合條件的商品</td>";
      body.appendChild(tr);
      return;
    }
    rows.forEach((item) => {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td>" + escapeHtml(item.name) + "</td><td>" + escapeHtml(item.quality) +
        "</td><td>" + escapeHtml(String(item.price)) + "</td><td>" + escapeHtml(String(item.id)) + "</td>";
      body.appendChild(tr);
    });
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[ch]));
  }

  function unitBounds(hunt) {
    if (hunt.bounds) return hunt.bounds;
    const pts = [];
    if (hunt.x != null && hunt.y != null) pts.push([hunt.x, hunt.y]);
    (hunt.monsters || []).forEach((mon) => { if (mon.x != null && mon.y != null) pts.push([mon.x, mon.y]); });
    (hunt.peers || []).forEach((peer) => { if (peer.x != null && peer.y != null) pts.push([peer.x, peer.y]); });
    if (hunt.manual_goal) pts.push(hunt.manual_goal);
    if (!pts.length) return null;
    let x0 = pts[0][0], x1 = pts[0][0], y0 = pts[0][1], y1 = pts[0][1];
    pts.forEach((pt) => {
      if (pt[0] < x0) x0 = pt[0]; if (pt[0] > x1) x1 = pt[0];
      if (pt[1] < y0) y0 = pt[1]; if (pt[1] > y1) y1 = pt[1];
    });
    const pad = Math.max(400, (x1 - x0) * 0.15, (y1 - y0) * 0.15);
    return { x0: x0 - pad, y0: y0 - pad, x1: x1 + pad, y1: y1 + pad };
  }

  // ---- Minimap: 簡化 / 詳細 modes, zoom centred on self, settings in localStorage ----
  const MINI_ZOOM_MIN = 0.5, MINI_ZOOM_MAX = 8;
  const miniPrefs = (() => {
    let mode = "detail", zoom = 1;
    try {
      const m = window.localStorage.getItem("tabby.minimap.mode");
      if (m === "simple" || m === "detail") mode = m;
      const z = parseFloat(window.localStorage.getItem("tabby.minimap.zoom"));
      if (isFinite(z)) zoom = Math.min(MINI_ZOOM_MAX, Math.max(MINI_ZOOM_MIN, z));
    } catch (err) { /* storage blocked */ }
    return { mode: mode, zoom: zoom };
  })();
  function saveMiniPrefs() {
    try {
      window.localStorage.setItem("tabby.minimap.mode", miniPrefs.mode);
      window.localStorage.setItem("tabby.minimap.zoom", String(miniPrefs.zoom));
    } catch (err) { /* ignore */ }
  }
  let miniFrame = 0;
  function scheduleMinimap() {
    if (miniFrame || !lastState) return;
    miniFrame = window.requestAnimationFrame(() => { miniFrame = 0; drawMinimap(lastState); });
  }
  function applyMiniMode() {
    const card = $("minimap-card"), canvas = $("minimap");
    if (!card || !canvas) return;
    const detail = miniPrefs.mode === "detail";
    card.classList.toggle("is-detail", detail);
    card.classList.toggle("is-simple", !detail);
    if ($("btn-mini-simple")) $("btn-mini-simple").classList.toggle("is-on", !detail);
    if ($("btn-mini-detail")) $("btn-mini-detail").classList.toggle("is-on", detail);
    // Match the backing store to the displayed size so text stays crisp and undistorted.
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    const w = Math.round((canvas.clientWidth || 320) * dpr), h = Math.round((canvas.clientHeight || 220) * dpr);
    if (w > 0 && canvas.width !== w) canvas.width = w;
    if (h > 0 && canvas.height !== h) canvas.height = h;
    canvas._dpr = dpr;
    if ($("mini-zoom-text")) $("mini-zoom-text").textContent = miniPrefs.zoom.toFixed(miniPrefs.zoom < 1 ? 2 : 1) + "×";
  }
  function setMiniZoom(z) {
    miniPrefs.zoom = Math.min(MINI_ZOOM_MAX, Math.max(MINI_ZOOM_MIN, Math.round(z * 100) / 100));
    saveMiniPrefs(); applyMiniMode(); scheduleMinimap();
  }

  // Decoded terrain (walkable cells / obstacles from the game package + learned server blocks), per map.
  const navGrids = {};
  function navGridFor(mapId) {
    if (!mapId) return null;
    let g = navGrids[mapId];
    const now = Date.now();
    if (!g || (!g.loading && now - g.at > 30000)) {
      g = navGrids[mapId] = g || { at: 0, data: null, img: null };
      g.loading = true;
      fetch("/api/navgrid?map=" + mapId).then((r) => r.json()).then((d) => {
        g.data = d; g.img = buildNavImage(d); g.at = Date.now(); g.loading = false;
      }).catch(() => { g.at = Date.now(); g.loading = false; });
    }
    return g;
  }
  function buildNavImage(d) {
    if (!d || !d.cell_size || !d.cells || !d.cells.length) return null;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    d.cells.forEach((c) => { x0 = Math.min(x0, c[0]); y0 = Math.min(y0, c[1]); x1 = Math.max(x1, c[0]); y1 = Math.max(y1, c[1]); });
    x0 -= 1; y0 -= 1; x1 += 1; y1 += 1;
    const cw = x1 - x0 + 1, ch = y1 - y0 + 1;
    const off = document.createElement("canvas");
    off.width = cw; off.height = ch;
    const c2 = off.getContext("2d");
    c2.fillStyle = "#2a2f33";  // wall / cliff / water
    c2.fillRect(0, 0, cw, ch);
    c2.fillStyle = "#1d3a28";  // walkable ground
    d.cells.forEach((c) => c2.fillRect(c[0] - x0, c[1] - y0, 1, 1));
    return { canvas: off, wx: x0 * d.cell_size, wy: y0 * d.cell_size, size: d.cell_size, cw: cw, ch: ch };
  }
  function drawTerrain(ctx, g, sx, scale, detail, k, w, h) {
    if (!g || !g.data) return;
    const d = g.data;
    if (g.img) {
      const a = sx(g.img.wx, g.img.wy);
      ctx.save();
      ctx.imageSmoothingEnabled = false;
      ctx.globalAlpha = detail ? 0.95 : 0.75;
      ctx.drawImage(g.img.canvas, a[0], a[1], g.img.cw * g.img.size * scale, g.img.ch * g.img.size * scale);
      ctx.restore();
    }
    if (detail && d.obstacles) {
      ctx.fillStyle = "rgba(120,110,90,0.75)";
      d.obstacles.forEach((o) => {
        const p = sx(o[0], o[1]);
        if (p[0] < -20 || p[1] < -20 || p[0] > w + 20 || p[1] > h + 20) return;
        ctx.beginPath();
        ctx.ellipse(p[0], p[1], Math.max(1, o[2] * scale), Math.max(1, o[3] * scale), 0, 0, Math.PI * 2);
        ctx.fill();
      });
    }
    if (d.blocked && d.blocked.length) {
      const cs = d.learned_cell || 16;
      ctx.fillStyle = "rgba(220,80,70,0.55)";  // walls learned from server rejections
      d.blocked.forEach((c) => {
        const p = sx(c[0] * cs, c[1] * cs);
        const sz = Math.max(detail ? 2 : 1.5, cs * scale);
        if (p[0] < -sz || p[1] < -sz || p[0] > w || p[1] > h) return;
        ctx.fillRect(p[0], p[1], sz, sz);
      });
    }
  }

  function drawMinimap(state) {
    const canvas = $("minimap");
    if (!canvas || canvas.offsetParent === null) return;  // hidden tab: skip work
    applyMiniMode();
    const detail = miniPrefs.mode === "detail";
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height;
    const k = canvas._dpr || 1;  // CSS px -> canvas px
    ctx.fillStyle = "#0b1a12";
    ctx.fillRect(0, 0, w, h);
    const hunt = state.hunt || {};
    const bounds = unitBounds(hunt);
    const pad = 16;
    if (!bounds) { canvas._view = null; return; }
    const bw = Math.max(1, bounds.x1 - bounds.x0), bh = Math.max(1, bounds.y1 - bounds.y0);
    const scale = Math.min((w - pad * 2) / bw, (h - pad * 2) / bh) * miniPrefs.zoom;
    const cx = hunt.x != null ? hunt.x : (bounds.x0 + bounds.x1) / 2;
    const cy = hunt.y != null ? hunt.y : (bounds.y0 + bounds.y1) / 2;
    canvas._view = { cx: cx, cy: cy, s: scale };
    const sx = (x, y) => [w / 2 + (x - cx) * scale, h / 2 + (y - cy) * scale];
    const onScreen = (p, m) => p[0] > -m && p[0] < w + m && p[1] > -m && p[1] < h + m;
    drawTerrain(ctx, navGridFor(hunt.map_id), sx, scale, detail, k, w, h);

    if (detail) {
      // World-space grid: pick a step that is 40–120 screen px.
      let step = 64;
      while (step * scale < 40) step *= 2;
      while (step * scale > 120 && step > 16) step /= 2;
      ctx.strokeStyle = "rgba(80,120,90,0.22)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      const wx0 = cx - w / 2 / scale, wx1 = cx + w / 2 / scale, wy0 = cy - h / 2 / scale, wy1 = cy + h / 2 / scale;
      for (let gx = Math.floor(wx0 / step) * step; gx <= wx1; gx += step) { const X = sx(gx, 0)[0]; ctx.moveTo(X, 0); ctx.lineTo(X, h); }
      for (let gy = Math.floor(wy0 / step) * step; gy <= wy1; gy += step) { const Y = sx(0, gy)[1]; ctx.moveTo(0, Y); ctx.lineTo(w, Y); }
      ctx.stroke();
      const mb = hunt.map_bounds;
      if (mb) {
        const a = sx(mb.x0, mb.y0), b = sx(mb.x1, mb.y1);
        ctx.strokeStyle = "rgba(160,200,170,0.35)";
        ctx.lineWidth = 2;
        ctx.strokeRect(a[0], a[1], b[0] - a[0], b[1] - a[1]);
      }
      ctx.fillStyle = "rgba(231,238,232,0.55)";
      ctx.font = Math.round(10 * k) + "px sans-serif";
      ctx.fillText("格線 " + step + " 單位", 6 * k, 14 * k);
    }

    const route = hunt.route || [];
    if (detail && route.length && hunt.x != null) {
      ctx.strokeStyle = "#7aa8ff";
      ctx.lineWidth = 2 * k;
      ctx.beginPath();
      const start = sx(hunt.x, hunt.y);
      ctx.moveTo(start[0], start[1]);
      route.forEach((pt) => { const p = sx(pt[0], pt[1]); ctx.lineTo(p[0], p[1]); });
      ctx.stroke();
    }

    // Monster clusters (怪堆): current one highlighted; others only in 詳細.
    (hunt.clusters || []).forEach((c) => {
      if (!detail && !c.current) return;
      const p = sx(c.x, c.y);
      const rr = Math.max(c.r * scale + 6 * k, 8 * k);
      if (!onScreen(p, rr + 40)) return;
      ctx.strokeStyle = c.current ? "rgba(255,207,90,0.85)" : "rgba(180,210,190,0.35)";
      ctx.lineWidth = (c.current ? 2 : 1) * k;
      ctx.setLineDash([6 * k, 5 * k]);
      ctx.beginPath(); ctx.arc(p[0], p[1], rr, 0, Math.PI * 2); ctx.stroke();
      ctx.setLineDash([]);
      if (detail) {
        ctx.fillStyle = c.current ? "#ffe39a" : "rgba(220,230,224,0.7)";
        ctx.font = Math.round(10 * k) + "px sans-serif";
        const label = "怪堆 " + c.alive + " 隻" + (c.respawn != null ? " · 重生≈" + c.respawn + "s" : "");
        ctx.fillText(label, p[0] - ctx.measureText(label).width / 2, p[1] - rr - 4 * k);
      }
    });

    const targetLv = hunt.farm_target_level != null ? hunt.farm_target_level :
      ((state.options && state.options.farm_target_level) || null);
    const labelFont = Math.round(11 * k) + "px sans-serif";
    const r = (detail ? 3.5 : 3) * k;
    // Non-target monsters first so target-level / elites draw on top.
    const mons = (hunt.monsters || []).filter((m) => m.x != null && m.alive);
    const rank = (m) => (m.elite ? 2 : (targetLv != null && m.level === targetLv ? 1 : 0));
    mons.sort((a, b) => rank(a) - rank(b));
    mons.forEach((mon) => {
      const p = sx(mon.x, mon.y);
      if (!onScreen(p, 60)) return;
      const onTarget = targetLv != null && mon.level === targetLv;
      if (mon.elite) {
        ctx.fillStyle = "#efbf4a";
        ctx.save(); ctx.translate(p[0], p[1]); ctx.rotate(Math.PI / 4);
        ctx.fillRect(-r * 1.2, -r * 1.2, r * 2.4, r * 2.4);
        ctx.restore();
      } else {
        ctx.fillStyle = onTarget ? "#5fd38d" : (detail ? "#e16d6d" : "rgba(225,109,109,0.55)");
        ctx.beginPath(); ctx.arc(p[0], p[1], onTarget ? r * 1.15 : r * 0.85, 0, Math.PI * 2); ctx.fill();
      }
      if (detail) {
        const label = (mon.name ? mon.name + " " : "") + (mon.level != null ? "Lv" + mon.level : "");
        if (label) {
          ctx.font = (onTarget || mon.elite ? "bold " : "") + labelFont;
          ctx.fillStyle = mon.elite ? "#ffe39a" : (onTarget ? "#9dffc3" : "#f3d9d9");
          ctx.fillText(label, p[0] + r + 4, p[1] + 4 * k);
        }
      }
    });

    (hunt.peers || []).forEach((peer) => {
      if (peer.x == null) return;
      const p = sx(peer.x, peer.y);
      if (!onScreen(p, 60)) return;
      const npc = peer.kind === "npc";
      ctx.fillStyle = npc ? "#c9a0ff" : "#7aa8ff";
      ctx.beginPath(); ctx.arc(p[0], p[1], r, 0, Math.PI * 2); ctx.fill();
      if (detail) {
        ctx.fillStyle = "#e7eee8";
        ctx.font = labelFont;
        ctx.fillText(peer.name || (npc ? "NPC" : "玩家"), p[0] + r + 4, p[1] + 4 * k);
      }
    });

    const portals = (hunt.portals || []).slice();
    if (hunt.portal && hunt.portal.length >= 2 && !portals.some((g) => g.x === hunt.portal[0] && g.y === hunt.portal[1])) {
      portals.push({ x: hunt.portal[0], y: hunt.portal[1], name: "" });
    }
    portals.forEach((g) => {
      let p = sx(g.x, g.y);
      const off = !onScreen(p, 0);
      if (off && !detail) return;
      if (off) p = [Math.min(w - 10, Math.max(10, p[0])), Math.min(h - 10, Math.max(10, p[1]))];  // pin to edge
      const sz = 5 * k;
      ctx.strokeStyle = "#7aa8ff";
      ctx.lineWidth = 2;
      ctx.strokeRect(p[0] - sz, p[1] - sz, sz * 2, sz * 2);
      if (detail && g.name) {
        ctx.fillStyle = "#b9d0ff";
        ctx.font = labelFont;
        const text = "→ " + g.name;
        const tw = ctx.measureText(text).width;
        const tx = Math.min(w - tw - 4, Math.max(4, p[0] + sz + 4));
        ctx.fillText(text, tx, p[1] + 4 * k);
      }
    });

    if (hunt.manual_goal) {
      const p = sx(hunt.manual_goal[0], hunt.manual_goal[1]);
      ctx.strokeStyle = "#e4c25a";
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(p[0], p[1], 8 * k, 0, Math.PI * 2); ctx.stroke();
    }

    if (hunt.x != null) {
      const p = sx(hunt.x, hunt.y);
      const tgt = hunt.target;
      if (detail && tgt && tgt.x != null) {
        const q = sx(tgt.x, tgt.y);
        ctx.strokeStyle = "#ffcf5a";
        ctx.lineWidth = 2 * k;
        ctx.setLineDash([8 * k, 6 * k]);
        ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
        ctx.setLineDash([]);
        ctx.beginPath(); ctx.arc(q[0], q[1], r * 2.2, 0, Math.PI * 2); ctx.stroke();
        // Attack reach ring around you; kite keep-away ring while kiting.
        if (tgt.reach) {
          ctx.strokeStyle = "rgba(255,207,90,0.45)";
          ctx.lineWidth = 1.5;
          ctx.setLineDash([4, 6]);
          ctx.beginPath(); ctx.arc(p[0], p[1], tgt.reach * scale, 0, Math.PI * 2); ctx.stroke();
          ctx.setLineDash([]);
        }
        if (tgt.kiting && tgt.kite_min) {
          ctx.strokeStyle = "rgba(255,120,120,0.6)";
          ctx.lineWidth = 1.5;
          ctx.setLineDash([2, 4]);
          ctx.beginPath(); ctx.arc(p[0], p[1], tgt.kite_min * scale, 0, Math.PI * 2); ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = "#ffb0b0";
          ctx.font = labelFont;
          ctx.fillText("風箏中", p[0] - 20 * k, p[1] - tgt.kite_min * scale - 6);
        }
      }
      ctx.fillStyle = "#6ee28a";
      ctx.strokeStyle = "#0b1a12";
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(p[0], p[1], (detail ? 5.5 : 4.5) * k, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      if (detail) {
        ctx.fillStyle = "#e7eee8";
        ctx.font = "bold " + Math.round(12 * k) + "px sans-serif";
        ctx.fillText("你", p[0] + 9 * k, p[1] + 4 * k);
      }
    }
  }

  function miniScreenToWorld(canvas, clientX, clientY) {
    const view = canvas._view;
    if (!view) return null;
    const rect = canvas.getBoundingClientRect();
    const px = (clientX - rect.left) / rect.width * canvas.width;
    const py = (clientY - rect.top) / rect.height * canvas.height;
    return [view.cx + (px - canvas.width / 2) / view.s, view.cy + (py - canvas.height / 2) / view.s];
  }


  function renderChat(state) {
    const log = $("chat-log");
    if (!log) return;
    const rows = state.chat || (state.hunt && state.hunt.chat) || [];
    if (!rows.length) {
      log.textContent = "連線後會顯示收到的世界訊息。只有你按送出才會發言。";
      return;
    }
    log.textContent = rows.map((row) => {
      const channel = row.channel || "世界";
      const sender = row.sender || "";
      return "[" + channel + "] " + sender + "：" + (row.text || "");
    }).join("\n");
    log.scrollTop = log.scrollHeight;
  }

  let bagTab = "ALL";
  let selectedItemKey = null;

  function itemKey(item) { return item ? String(item.id) + "|" + (item.equipped ? "e" : "b") : null; }

  function iconHtml(item, cls) {
    if (!item) return "";
    if (item.icon) return "<img src=\"" + escapeHtml(item.icon) + "\" alt=\"\" class=\"" + (cls || "") + "\" />";
    return "<span class=\"emoji\">" + escapeHtml(item.emoji || "📦") + "</span>";
  }

  function fmtNum(v, suffix) {
    if (v === null || v === undefined || v === "") return "—";
    return String(v) + (suffix || "");
  }

  function renderCharacter(state) {
    const data = state.character || {};
    if ($("character-status")) $("character-status").textContent = data.error || data.status || "尚未讀取角色資料";
    const page = data.page;
    if (!page) return;
    const info = page.info || {};
    $("ci-name").textContent = info.name || "—";
    $("ci-level").textContent = "等級：" + fmtNum(info.level) + (info.class_name ? "　" + info.class_name : "");
    if (info.portrait && $("ci-portrait").getAttribute("src") !== info.portrait) $("ci-portrait").setAttribute("src", info.portrait);
    const exp = Number(info.experience), need = Number(info.experience_needed);
    $("ci-exp-text").textContent = "經驗 " + fmtNum(info.experience) + " / " + fmtNum(info.experience_needed);
    $("ci-exp-fill").style.width = (need > 0 && exp >= 0 ? Math.min(100, exp / need * 100) : 0) + "%";
    const row = (icon, label, value) => "<div class=\"row\"><span>" + icon + " " + escapeHtml(label) + "</span><strong>" + escapeHtml(value) + "</strong></div>";
    const hp = info.max_health != null ? fmtNum(info.health) + " / " + fmtNum(info.max_health) : fmtNum(info.health);
    const pct = (v) => (v === null || v === undefined ? "—" : (Number(v) >= 0 ? "+" : "") + v + "%");
    $("ci-stats").innerHTML = row("❤️", "生命", hp) + row("🗡️", "攻擊", fmtNum(info.attack)) +
      row("🛡️", "防禦", fmtNum(info.defense)) + row("🥾", "攻擊速度", pct(info.attack_speed)) +
      row("💨", "移動速度", pct(info.move_speed));
    $("ci-power").textContent = fmtNum(info.combat_power);
    $("ci-source").textContent = info.stats_source === "live" ? "數值來自即時連線。" :
      "尚未連線遊戲：攻擊、防禦、生命等目前只顯示裝備加成；開始連線後會換成完整數值。藥水等道具也要連線後才會出現。";
    $("ci-stats2").innerHTML = row("😈", "惡名值", fmtNum(info.evil) + (info.evil_max != null ? " / " + info.evil_max : "")) +
      row("➕", "每秒回血", info.health_regen != null ? "+" + info.health_regen : "—");

    const gear = page.gear || [];
    $("gear-row").innerHTML = gear.map((slot) => {
      const it = slot.item;
      const sel = it && itemKey(it) === selectedItemKey ? " is-selected" : "";
      const q = it && it.quality_code ? " q-" + it.quality_code : "";
      return "<div class=\"slot-box" + (it ? "" : " empty") + sel + q + "\" data-item-key=\"" + escapeHtml(itemKey(it) || "") +
        "\" title=\"" + escapeHtml(it ? it.name : slot.slot_name) + "\">" + (it ? iconHtml(it) : "＋") +
        "<span class=\"slot-label\">" + escapeHtml(slot.slot_name) + "</span></div>";
    }).join("");

    const tabs = page.tabs || [];
    $("bag-tabs").innerHTML = tabs.map((t) => "<button type=\"button\" class=\"secondary" + (t.key === bagTab ? " is-on" : "") +
      "\" data-bag-tab=\"" + t.key + "\">" + escapeHtml(t.label) + " " + t.count + "</button>").join("");
    const bag = (page.bag || []).filter((it) => bagTab === "ALL" || it.category === bagTab);
    const cells = bag.map((it) => {
      const sel = itemKey(it) === selectedItemKey ? " is-selected" : "";
      const q = it.quality_code ? " q-" + it.quality_code : "";
      const qty = Number(it.quantity) > 1 || it.virtual ? "<span class=\"qty\">" + escapeHtml(String(it.quantity)) + "</span>" : "";
      return "<div class=\"bag-cell" + sel + q + "\" data-item-key=\"" + escapeHtml(itemKey(it)) + "\" title=\"" + escapeHtml(it.name) + "\">" +
        iconHtml(it) + qty + "</div>";
    });
    while (cells.length < 24) cells.push("<div class=\"bag-cell empty\"></div>");
    $("bag-grid").innerHTML = cells.join("");
    renderItemDetail(page);
  }

  function findItem(page, key) {
    if (!key) return null;
    for (const slot of (page.gear || [])) if (slot.item && itemKey(slot.item) === key) return slot.item;
    for (const it of (page.bag || [])) if (itemKey(it) === key) return it;
    return null;
  }

  function renderItemDetail(page) {
    const item = findItem(page, selectedItemKey);
    $("detail-empty").hidden = !!item;
    $("detail-body").hidden = !item;
    if (!item) return;
    $("detail-icon").innerHTML = iconHtml(item);
    $("detail-name").textContent = item.name || "物品";
    const tag = [item.slot_name || "其他", item.quality && item.quality !== "—" ? item.quality : "", item.level != null ? "Lv" + item.level : ""]
      .filter(Boolean).join(" · ");
    $("detail-tag").textContent = tag + (item.equipped ? "（已裝備）" : "");
    $("detail-desc").textContent = item.description || "";
    const attrs = item.attributes || [];
    $("detail-attrs").innerHTML = attrs.length ? attrs.map((a) => "<div>" + escapeHtml(a.text) + "</div>").join("") :
      (item.category === "OTHER" ? "" : "<div class=\"tiny\">沒有額外屬性資料</div>");
    // Compare with the equipped item in the same slot.
    let compare = "";
    if (!item.equipped && item.slot && item.category !== "OTHER") {
      const slot = (page.gear || []).find((g) => g.slot === item.slot);
      const cur = slot && slot.item;
      const sum = (list) => { const m = {}; (list || []).forEach((a) => { m[a.code] = (m[a.code] || 0) + Number(a.value || 0); m[a.code + "_n"] = a.name; m[a.code + "_s"] = a.suffix; }); return m; };
      const mine = sum(item.attributes), theirs = sum(cur ? cur.attributes : []);
      const codes = Array.from(new Set(Object.keys(mine).concat(Object.keys(theirs)).filter((k) => !k.endsWith("_n") && !k.endsWith("_s"))));
      compare = "<div class=\"tiny\">與目前裝備比較（" + escapeHtml(cur ? cur.name : "空欄位") + "）</div>" + codes.map((c) => {
        const d = (mine[c] || 0) - (theirs[c] || 0);
        if (!d) return "";
        const name = mine[c + "_n"] || theirs[c + "_n"] || c, suf = mine[c + "_s"] || theirs[c + "_s"] || "";
        return "<div class=\"" + (d > 0 ? "up" : "down") + "\">" + escapeHtml(name) + " " + (d > 0 ? "▲ +" : "▼ ") + d + suf + "</div>";
      }).join("");
    }
    $("detail-compare").innerHTML = compare;
    const actions = [];
    if (item.equipped && item.can_unequip) {
      actions.push("<button type=\"button\" class=\"secondary wide\" data-unequip=\"" + escapeHtml(item.slot) + "\" data-name=\"" + escapeHtml(item.name) + "\">卸下</button>");
    } else if (item.can_equip) {
      actions.push("<button type=\"button\" class=\"primary wide\" data-equip=\"" + escapeHtml(String(item.id)) + "\" data-name=\"" + escapeHtml(item.name) + "\">裝備</button>");
    }
    if (item.use_kind) {
      actions.push("<button type=\"button\" class=\"primary wide\" data-use=\"" + escapeHtml(item.use_kind) + "\" data-name=\"" + escapeHtml(item.name) + "\">使用</button>");
    } else if (item.use_note) {
      actions.push("<p class=\"tiny\">" + escapeHtml(item.use_note) + "</p>");
    }
    if (item.locked || item.listed) actions.push("<p class=\"tiny\">此物品已鎖定或上架中，不能裝備。</p>");
    $("detail-actions").innerHTML = actions.join("");
  }

  document.addEventListener("click", (ev) => {
    const el = ev.target && ev.target.closest ? ev.target.closest("[data-item-key],[data-bag-tab]") : null;
    if (!el) return;
    if (el.hasAttribute("data-bag-tab")) { bagTab = el.getAttribute("data-bag-tab"); }
    else if (el.getAttribute("data-item-key")) { selectedItemKey = el.getAttribute("data-item-key"); }
    else return;
    if (lastState) renderCharacter(lastState);
  });
  document.addEventListener("click", (ev) => {
    const kind = ev.target && ev.target.getAttribute && ev.target.getAttribute("data-use");
    if (!kind) return;
    const name = ev.target.getAttribute("data-name") || "道具";
    useConsumable(kind, "使用" + name, "確定要使用 1 個「" + name + "」嗎？");
  });

  let confirmResolver = null;
  function askConfirm(title, message) {
    return new Promise((resolve) => {
      const modal = $("confirm-modal");
      $("confirm-title").textContent = title;
      $("confirm-text").textContent = message;
      modal.hidden = false;
      confirmResolver = resolve;
    });
  }
  function finishConfirm(ok) {
    const modal = $("confirm-modal");
    if (modal) modal.hidden = true;
    const resolve = confirmResolver;
    confirmResolver = null;
    if (resolve) resolve(ok);
  }

  function applyState(state) {
    lastState = state;
    const gate = $("gate");
    const workspace = $("workspace");
    const unlocked = !!state.unlocked;
    gate.hidden = unlocked;
    workspace.hidden = !unlocked;
    $("btn-lock").hidden = !unlocked;
    if ($("auto-unlock-wrap")) $("auto-unlock-wrap").hidden = !unlocked;
    if ($("opt-auto-unlock") && document.activeElement !== $("opt-auto-unlock")) {
      $("opt-auto-unlock").checked = state.auto_unlock !== false;
    }
    if (!state.has_vault) {
      $("gate-title").textContent = "首次設定";
      $("gate-hint").textContent = "把專用錢包的私鑰或助記詞貼在下面，並設定一個只有你知道的主密碼。";
      $("form-setup").hidden = false;
      $("form-unlock").hidden = true;
    } else if (!unlocked) {
      $("gate-title").textContent = "先解鎖錢包";
      $("gate-hint").textContent = "第一次請輸入主密碼。勾選「記住此電腦」後，下次開啟會自動解鎖。";
      $("form-setup").hidden = true;
      $("form-unlock").hidden = false;
      if ($("unlock-remember")) $("unlock-remember").checked = true;
    }
    const pill = $("pill");
    pill.textContent = state.pill || "尚未解鎖";
    pill.classList.toggle("is-on", !!state.connected);
    selectedMap = (state.options && state.options.map_id) || selectedMap;
    if ($("opt-elite")) $("opt-elite").checked = !!(state.options && state.options.elite_only);
    if ($("opt-auto") && state.options) $("opt-auto").checked = state.options.auto_combat !== false;
    if ($("opt-return") && state.options) $("opt-return").checked = state.options.auto_return !== false;
    if (state.daily_running) {
      if ($("opt-auto-quest") && document.activeElement !== $("opt-auto-quest")) {
        $("opt-auto-quest").checked = !!(state.options && state.options.auto_quest);
      }
      if ($("opt-task-limited") && document.activeElement !== $("opt-task-limited")) {
        $("opt-task-limited").checked = !!(state.options && state.options.task_limited);
      }
      if ($("opt-quest-map") && document.activeElement !== $("opt-quest-map")) {
        $("opt-quest-map").checked = !!(state.options && state.options.quest_map_auto);
      }
      if ($("opt-quest-reroll") && document.activeElement !== $("opt-quest-reroll")) {
        $("opt-quest-reroll").checked = !!(state.options && state.options.quest_reroll);
      }
      if ($("opt-skip-vendetta") && document.activeElement !== $("opt-skip-vendetta")) {
        $("opt-skip-vendetta").checked = !!(state.options && state.options.skip_vendetta);
      }
      if ($("no-kite-level") && document.activeElement !== $("no-kite-level") && state.options && state.options.no_kite_level != null) {
        $("no-kite-level").value = String(state.options.no_kite_level);
      }
      if ($("opt-auto-replace") && document.activeElement !== $("opt-auto-replace")) {
        $("opt-auto-replace").checked = !!(state.options && state.options.auto_replace);
      }
      if ($("opt-auto-farm") && document.activeElement !== $("opt-auto-farm")) {
        $("opt-auto-farm").checked = !(state.options && state.options.auto_farm === false);
      }
      if ($("farm-level") && document.activeElement !== $("farm-level")) {
        $("farm-level").value = String((state.options && state.options.farm_target_level) || 30);
      }
    } else {
      if ($("opt-auto-farm") && document.activeElement !== $("opt-auto-farm") && state.options) {
        $("opt-auto-farm").checked = state.options.auto_farm !== false;
      }
      if ($("farm-level") && document.activeElement !== $("farm-level") && state.options && state.options.farm_target_level) {
        $("farm-level").value = String(state.options.farm_target_level);
      }
    }
    if ($("market-name") && document.activeElement !== $("market-name")) {
      $("market-name").value = (state.options && state.options.name_contains) || "";
    }
    if ($("market-max") && document.activeElement !== $("market-max")) {
      $("market-max").value = (state.options && state.options.max_price) || 200;
    }
    const hunt = state.hunt || {};
    $("current-map").textContent = "目前所在地圖　" + (hunt.map_title || hunt.map_name || "尚未連線") +
      (hunt.map_level_text ? "（" + hunt.map_level_text + "）" : "");
    if (hunt.x != null) {
      $("coords").textContent = (hunt.estimated ? "預計座標" : "座標") + "　(" + hunt.x + ", " + hunt.y + ")";
    } else {
      $("coords").textContent = "座標　—";
    }
    $("stat-kills").textContent = hunt.kills || 0;
    $("stat-damage").textContent = hunt.damage || 0;
    $("stat-hits").textContent = hunt.hits || 0;
    $("stat-potions").textContent = hunt.potions || 0;
    {
      const st = ((state.character || {}).stats || []);
      const pick = (k) => { const r = st.find((x) => x && x.key === k); return r ? r.value : "—"; };
      if ($("stat-stamina")) $("stat-stamina").textContent = pick("stamina");
      if ($("stat-exp")) $("stat-exp").textContent = pick("experience");
      const ss = state.session || {};
      if ($("session-stats")) $("session-stats").textContent = ss.kills == null ? "本次掛機：—" :
        "本次掛機 " + ss.minutes + " 分鐘　擊殺 " + ss.kills + "　經驗 +" + ss.exp + "　疲勞消耗 " + Number(ss.stamina_used).toFixed(2) + "%　裝備 +" + ss.equipment;
    }
    $("combat-feed").textContent = hunt.status_line || hunt.status || "等待開始打怪";
    if ($("farm-info")) {
      const parts = [];
      if (hunt.level_filter) parts.push("等級篩選：" + hunt.level_filter);
      if (hunt.cluster_note) parts.push(hunt.cluster_note);
      if (hunt.quest_map_target) parts.push("任務優先：前往任務地圖");
      else if (hunt.farm_map_name) parts.push("練功地圖：" + hunt.farm_map_name + (hunt.farm_reason ? "（" + hunt.farm_reason + "）" : ""));
      $("farm-info").textContent = parts.join("　·　") || "—";
    }
    $("mini-caption").textContent = (hunt.map_title || "尚未進入地圖") +
      (hunt.activity ? "　" + hunt.activity : "") +
      (hunt.health != null ? "　生命 " + hunt.health + "/" + (hunt.max_health || "—") : "");
    renderMaps(state);
    renderLogs(state);
    renderQuests(state);
    renderDelivery(state);
    renderMarket(state);
    drawMinimap(state);
    try { renderCharacter(state); } catch (err) { console.error(err); }
    renderAlert(state);
    renderCombat(state);
    renderEfficiency(state);
    renderWebhook(state);
    if (state.hunt_error) toast(state.hunt_error);
    if (state.market_error) toast(state.market_error);
  }

  function renderWebhook(state) {
    const hook = state.webhook || {};
    const url = $("webhook-url");
    if (url && document.activeElement !== url && !url.dataset.dirty) url.value = hook.url || "";
    const key = $("webhook-key");
    if (key && document.activeElement !== key && !key.value) key.placeholder = hook.has_key ? "已儲存（留空表示不變）" : "尚未設定";
    if ($("webhook-status") && hook.last_result) $("webhook-status").textContent = "最近一次：" + hook.last_result;
  }

  function renderCombat(state) {
    const c = state.combat || {};
    const map = { "opt-no-potion": "no_potion", "opt-potion": "auto_potion", "opt-potion-pvp": "potion_on_player_damage",
      "opt-prefer-elite": "prefer_elite" };
    Object.keys(map).forEach((id) => {
      const el = $(id);
      if (el && document.activeElement !== el && c[map[id]] !== undefined) el.checked = !!c[map[id]];
    });
    if ($("opt-cluster") && document.activeElement !== $("opt-cluster") && c.cluster_kite !== undefined) $("opt-cluster").checked = !!c.cluster_kite;
    if ($("opt-kite-elite") && document.activeElement !== $("opt-kite-elite") && c.kite_elite_only !== undefined) $("opt-kite-elite").checked = !!c.kite_elite_only;
    if ($("max-pull") && document.activeElement !== $("max-pull") && c.max_pull) $("max-pull").value = String(c.max_pull);
    if ($("cluster-note") && (state.hunt || {}).cluster_note) $("cluster-note").textContent = state.hunt.cluster_note;
    const num = $("elite-potion-min");
    if (num && document.activeElement !== num && c.elite_potion_min !== undefined) num.value = c.elite_potion_min;
    const view = $("elite-view-range");
    if (view && document.activeElement !== view && c.elite_view_range !== undefined) view.value = c.elite_view_range;
    const hunt = state.hunt || {};
    const notes = [hunt.elite_note, hunt.kite_note, hunt.potion_note].filter(Boolean);
    if ($("elite-note") && notes.length) $("elite-note").textContent = notes.join("　·　");
  }

  function renderEfficiency(state) {
    const eff = state.efficiency || {};
    const body = $("eff-body");
    if (!body) return;
    const rows = eff.rows || [];
    const fmt = (v) => (v === null || v === undefined ? "—" : String(v));
    body.textContent = "";
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 7; td.textContent = "還沒有資料";
      tr.appendChild(td); body.appendChild(tr);
    }
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      if (r.best) tr.classList.add("is-best");
      if (!r.enough) tr.classList.add("is-few");
      const label = r.label + " · " + r.map_name + (r.enough ? "" : "（樣本不足）") + (r.best ? " ★最佳" : "");
      [label, r.kills, fmt(r.avg_exp), fmt(r.avg_secs), fmt(r.exp_per_min), fmt(r.potions_per_kill), r.aborts].forEach((v) => {
        const td = document.createElement("td");
        td.textContent = String(v);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    if ($("eff-note")) {
      $("eff-note").textContent = "從 " + (eff.since || "—") + " 開始記錄 · 移動 " + (eff.travel_secs || 0) +
        " 秒 · 閒置/巡邏 " + (eff.idle_secs || 0) + " 秒 · 只用實際量到的數據，擊殺少於 " + (eff.min_samples || 5) + " 隻標示「樣本不足」。";
    }
  }

  async function saveCombat() {
    try {
      await api("/api/combat/options", {
        no_potion: !!($("opt-no-potion") && $("opt-no-potion").checked),
        auto_potion: $("opt-potion").checked,
        potion_on_player_damage: $("opt-potion-pvp").checked,
        prefer_elite: $("opt-prefer-elite").checked,
        elite_potion_min: parseInt($("elite-potion-min").value || "0", 10),
        elite_view_range: parseInt(($("elite-view-range") && $("elite-view-range").value) || "700", 10),
        cluster_kite: $("opt-cluster") ? $("opt-cluster").checked : true,
        kite_elite_only: $("opt-kite-elite") ? $("opt-kite-elite").checked : false,
        max_pull: 1,
      });
    } catch (err) { toast(String(err.message || err)); }
  }

  function renderAlert(state) {
    const banner = $("alert-banner");
    if (!banner) return;
    const alert = state.alert;
    if (!alert) { banner.hidden = true; return; }
    banner.hidden = false;
    banner.classList.toggle("is-recovering", alert.level === "recovering");
    $("alert-text").textContent = "⚠ " + (alert.time ? alert.time + " " : "") + (alert.text || alert.reason || "外掛疑似卡住");
  }

  async function refresh() {
    try {
      const state = await api("/api/state");
      applyState(state);
    } catch (err) {
      toast(err.message);
    }
  }

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("is-on", el.dataset.tab === name));
    $("page-game").classList.toggle("is-on", name === "game");
    $("page-market").classList.toggle("is-on", name === "market");
    if ($("page-character")) $("page-character").classList.toggle("is-on", name === "character");
  }

  $("tab-game").addEventListener("click", () => switchTab("game"));
  $("tab-market").addEventListener("click", () => switchTab("market"));
  if ($("tab-character")) $("tab-character").addEventListener("click", () => switchTab("character"));
  $("btn-show-setup").addEventListener("click", () => {
    $("form-setup").hidden = false;
    $("form-unlock").hidden = true;
    $("gate-title").textContent = "首次設定";
  });
  $("setup-secret").addEventListener("input", async () => {
    const v = $("setup-secret").value.trim();
    const n = v.split(/\s+/).length;
    if (![12, 15, 18, 21, 24].includes(n)) { $("setup-addr-wrap").hidden = true; return; }
    try {
      const res = await api("/api/wallet/preview", { secret: v });
      const list = (res && res.addresses) || [];
      $("setup-addr").innerHTML = list.map((a, i) => '<option value="' + i + '">錢包 ' + (i + 1) + "：" + a + "</option>").join("");
      $("setup-addr-wrap").hidden = !list.length;
    } catch (err) { $("setup-addr-wrap").hidden = true; }
  });
  $("form-setup").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    showError("gate-error", "");
    try {
      await api("/api/setup", {
        secret: $("setup-secret").value.trim() + ($("setup-addr-wrap").hidden ? "" : " " + (Number($("setup-addr").value) + 1)),
        password: $("setup-pass").value,
        confirm: $("setup-confirm").value,
        remember: $("setup-remember") ? $("setup-remember").checked : true
      });
      $("setup-secret").value = "";
      $("setup-pass").value = "";
      $("setup-confirm").value = "";
      await refresh();
    } catch (err) { showError("gate-error", err.message); }
  });
  $("form-unlock").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    showError("gate-error", "");
    try {
      await api("/api/unlock", {
        password: $("unlock-pass").value,
        remember: $("unlock-remember") ? $("unlock-remember").checked : true
      });
      $("unlock-pass").value = "";
      await refresh();
    } catch (err) { showError("gate-error", err.message); }
  });
  $("opt-auto-unlock").addEventListener("change", async () => {
    try {
      await api("/api/auto-unlock", { enabled: $("opt-auto-unlock").checked });
      await refresh();
    } catch (err) {
      toast(err.message);
      $("opt-auto-unlock").checked = !$("opt-auto-unlock").checked;
    }
  });
  $("webhook-url").addEventListener("input", () => { $("webhook-url").dataset.dirty = "1"; });
  $("btn-webhook-save").addEventListener("click", async () => {
    try {
      await api("/api/webhook/save", { url: $("webhook-url").value.trim(), key: $("webhook-key").value });
      $("webhook-key").value = "";
      delete $("webhook-url").dataset.dirty;
      toast("通知設定已儲存");
      refresh();
    } catch (err) { toast(String(err.message || err)); }
  });
  $("btn-webhook-test").addEventListener("click", async () => {
    try {
      const res = await api("/api/webhook/test", {});
      toast(res.result || "已送出");
      refresh();
    } catch (err) { toast(String(err.message || err)); }
  });

  ["opt-no-potion", "opt-potion", "opt-potion-pvp", "opt-prefer-elite"].forEach((id) => {
    $(id).addEventListener("change", saveCombat);
  });
  $("elite-potion-min").addEventListener("change", saveCombat);
  if ($("elite-view-range")) $("elite-view-range").addEventListener("change", saveCombat);
  if ($("opt-cluster")) $("opt-cluster").addEventListener("change", saveCombat);
  if ($("opt-kite-elite")) $("opt-kite-elite").addEventListener("change", saveCombat);
  if ($("max-pull")) $("max-pull").addEventListener("change", saveCombat);
  $("btn-eff-reset").addEventListener("click", async () => {
    if (!window.confirm("確定要清空打怪效率紀錄嗎？")) return;
    try { await api("/api/efficiency/reset", {}); refresh(); } catch (err) { toast(String(err.message || err)); }
  });

  $("btn-alert-clear").addEventListener("click", async () => {
    try { await api("/api/alert/clear", {}); } catch (err) { toast(String(err.message || err)); }
    refresh();
  });

  $("btn-lock").addEventListener("click", async () => {
    try { await api("/api/lock", {}); await refresh(); } catch (err) { toast(err.message); }
  });
  $("btn-hunt-start").addEventListener("click", async () => {
    try {
      await api("/api/hunt/start", {
        map_id: selectedMap,
        elite_only: $("opt-elite").checked,
        auto_combat: $("opt-auto").checked,
        auto_return: $("opt-return").checked
      });
      toast(lastState && lastState.connected && !lastState.hunt_running ? "繼續打怪（未重新登入）" : "開始打怪");
      await refresh();
    } catch (err) { toast(err.message); }
  });
  $("btn-hunt-stop").addEventListener("click", async () => {
    try { await api("/api/hunt/stop", {}); toast("已停止打怪（不會登出）"); await refresh(); } catch (err) { toast(err.message); }
  });
  $("btn-hunt-stop-now").addEventListener("click", async () => {
    try { await api("/api/hunt/stop-now", {}); toast("已立刻停止（不會登出）"); await refresh(); } catch (err) { toast(err.message); }
  });
  $("btn-stop-move").addEventListener("click", async () => {
    try { await api("/api/hunt/stop-move", {}); await refresh(); } catch (err) { toast(err.message); }
  });
  ["opt-elite", "opt-auto", "opt-return"].forEach((id) => {
    $(id).addEventListener("change", async () => {
      try {
        await api("/api/hunt/options", {
          elite_only: $("opt-elite").checked,
          auto_combat: $("opt-auto").checked,
          auto_return: $("opt-return").checked,
          map_id: selectedMap
        });
      } catch (err) { toast(err.message); }
    });
  });
  async function postQuestOptions() {
    await api("/api/quests/options", {
      auto_quest: $("opt-auto-quest").checked,
      task_limited: $("opt-task-limited").checked,
      quest_map_auto: $("opt-quest-map").checked,
      auto_replace: $("opt-auto-replace").checked,
      skip_vendetta: $("opt-skip-vendetta").checked, quest_reroll: $("opt-quest-reroll") ? $("opt-quest-reroll").checked : false,
      no_kite_level: $("no-kite-level") ? Number($("no-kite-level").value) : 30,
      auto_farm: $("opt-auto-farm") ? $("opt-auto-farm").checked : true,
      farm_target_level: $("farm-level") ? Number($("farm-level").value || 30) : 30
    });
  }
  $("btn-quest-refresh").addEventListener("click", async () => {
    try {
      await api("/api/quests/refresh", {});
      toast("已重新整理任務");
      await refresh();
    } catch (err) { toast(err.message); }
  });
  $("btn-daily-start").addEventListener("click", async () => {
    try {
      await api("/api/quests/start", {
        map_id: selectedMap,
        auto_quest: $("opt-auto-quest").checked,
        task_limited: $("opt-task-limited").checked,
        quest_map_auto: $("opt-quest-map").checked,
        auto_replace: $("opt-auto-replace").checked,
        skip_vendetta: $("opt-skip-vendetta").checked, quest_reroll: $("opt-quest-reroll") ? $("opt-quest-reroll").checked : false,
      no_kite_level: $("no-kite-level") ? Number($("no-kite-level").value) : 30,
        no_kite_level: $("no-kite-level") ? Number($("no-kite-level").value) : 30,
        auto_farm: $("opt-auto-farm") ? $("opt-auto-farm").checked : true,
        farm_target_level: $("farm-level") ? Number($("farm-level").value || 30) : 30
      });
      toast("開始每日任務");
      await refresh();
    } catch (err) { toast(err.message); }
  });
  $("btn-daily-stop").addEventListener("click", async () => {
    try { await api("/api/quests/stop", {}); await refresh(); } catch (err) { toast(err.message); }
  });
  $("btn-delivery-confirm").addEventListener("click", async () => {
    try {
      await api("/api/quests/deliver", {});
      toast("已確認交付");
      await refresh();
    } catch (err) { toast(err.message); await refresh(); }
  });
  $("btn-delivery-cancel").addEventListener("click", async () => {
    if (!confirm("取消任務也會佔用每日領取次數，確定取消？")) return;
    try { await api("/api/quests/cancel", {}); toast("已取消任務"); await refresh(); }
    catch (err) { toast(err.message); await refresh(); }
  });
  $("btn-delivery-close").addEventListener("click", () => {
    const st = window.__lastState || {};
    window.__deliveryClosed = (st.pending_delivery || {}).quest_id;
    $("delivery-modal").hidden = true;
  });
  $("btn-delivery-skip").addEventListener("click", async () => {
    try {
      await api("/api/quests/skip", {});
      await refresh();
    } catch (err) { toast(err.message); await refresh(); }
  });
  ["opt-auto-quest", "opt-task-limited", "opt-quest-map", "opt-auto-replace", "opt-skip-vendetta", "opt-quest-reroll", "opt-auto-farm", "farm-level", "no-kite-level"].forEach((id) => {
    if ($(id)) $(id).addEventListener("change", async () => {
      try { await postQuestOptions(); } catch (err) { toast(err.message); }
    });
  });
  $("btn-market-start").addEventListener("click", async () => {
    try {
      await api("/api/market/start", {
        name_contains: $("market-name").value,
        max_price: Number($("market-max").value || 200)
      });
      toast("開始監控商城");
      await refresh();
    } catch (err) { toast(err.message); }
  });
  $("btn-market-stop").addEventListener("click", async () => {
    try { await api("/api/market/stop", {}); await refresh(); } catch (err) { toast(err.message); }
  });

  if ($("minimap")) {
    $("minimap").addEventListener("click", async (ev) => {
      const canvas = $("minimap");
      if (!canvas._view || !lastState || !lastState.connected) {
        toast("請先連線，才能在小地圖上移動。");
        return;
      }
      const pt = miniScreenToWorld(canvas, ev.clientX, ev.clientY);
      if (!pt) return;
      try {
        const result = await api("/api/hunt/move", { x: pt[0], y: pt[1] });
        toast(result.message || "開始移動");
        await refresh();
      } catch (err) { toast(err.message); }
    });
    $("minimap").addEventListener("wheel", (ev) => {
      ev.preventDefault();
      setMiniZoom(miniPrefs.zoom * (ev.deltaY < 0 ? 1.2 : 1 / 1.2));
    }, { passive: false });
  }
  if ($("btn-mini-in")) $("btn-mini-in").addEventListener("click", () => setMiniZoom(miniPrefs.zoom * 1.25));
  if ($("btn-mini-out")) $("btn-mini-out").addEventListener("click", () => setMiniZoom(miniPrefs.zoom / 1.25));
  document.querySelectorAll("[data-mini-mode]").forEach((btn) => btn.addEventListener("click", () => {
    miniPrefs.mode = btn.getAttribute("data-mini-mode");
    saveMiniPrefs(); applyMiniMode(); scheduleMinimap();
  }));
  applyMiniMode();
  async function useConsumable(kind, title, message) {
    const ok = await askConfirm(title, message);
    if (!ok) return;
    try {
      const result = await api("/api/item/use", { kind: kind, confirm: true });
      toast(result.message || "已送出");
      await refresh();
    } catch (err) { toast(err.message); }
  }
  if ($("btn-use-potion")) $("btn-use-potion").addEventListener("click", () =>
    useConsumable("health", "使用生命藥水", "確定要使用 1 瓶生命藥水嗎？"));
  if ($("btn-use-potion-char")) $("btn-use-potion-char").addEventListener("click", () =>
    useConsumable("health", "使用生命藥水", "確定要使用 1 瓶生命藥水嗎？"));
  if ($("btn-use-stamina")) $("btn-use-stamina").addEventListener("click", () =>
    useConsumable("stamina", "使用體力藥水", "確定要使用 1 瓶體力恢復藥水嗎？"));
  if ($("btn-confirm-ok")) $("btn-confirm-ok").addEventListener("click", () => finishConfirm(true));
  if ($("btn-confirm-cancel")) $("btn-confirm-cancel").addEventListener("click", () => finishConfirm(false));
  if ($("btn-character-refresh")) $("btn-character-refresh").addEventListener("click", async () => {
    try {
      await api("/api/character/refresh", {});
      toast("已重新整理角色資料");
      await refresh();
    } catch (err) { toast(err.message); }
  });
  document.addEventListener("click", async (ev) => {
    const equip = ev.target && ev.target.getAttribute && ev.target.getAttribute("data-equip");
    const unequip = ev.target && ev.target.getAttribute && ev.target.getAttribute("data-unequip");
    if (equip) {
      const name = ev.target.getAttribute("data-name") || "這件物品";
      const ok = await askConfirm("穿戴裝備", "確定要穿上「" + name + "」嗎？這不會自動出售或丟棄。");
      if (!ok) return;
      try {
        await api("/api/character/equip", { player_item_id: Number(equip) || equip, confirm: true });
        toast("已送出穿戴請求");
        await refresh();
      } catch (err) { toast(err.message); }
    }
    if (unequip) {
      const name = ev.target.getAttribute("data-name") || "這件裝備";
      const ok = await askConfirm("卸下裝備", "確定要卸下「" + name + "」嗎？");
      if (!ok) return;
      try {
        await api("/api/character/unequip", { slot_type: unequip, confirm: true });
        toast("已送出卸下請求");
        await refresh();
      } catch (err) { toast(err.message); }
    }
  });
  async function sendChat() {
    const input = $("chat-input");
    if (!input) return;
    const message = input.value.trim();
    if (!message) { toast("請先輸入訊息"); return; }
    try {
      await api("/api/chat/send", { message: message });
      input.value = "";
      await refresh();
    } catch (err) { toast(err.message); }
  }
  if ($("btn-chat-send")) $("btn-chat-send").addEventListener("click", sendChat);
  if ($("chat-input")) $("chat-input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); sendChat(); }
  });

  // 自動檢查 GitHub 新版本，有就顯示更新按鈕
  const updBar = document.getElementById("update-bar");
  const updText = document.getElementById("update-text");
  const updBtn = document.getElementById("update-btn");
  let updSkip = "";
  async function checkUpdate() {
    try {
      const d = await api("/api/update/check");
      if (d.available && d.remote !== updSkip) {
        updText.textContent = "有新版本可以更新（" + (d.local || "?") + " → " + d.remote + "）" + (d.message ? "：" + d.message : "");
        updBar.hidden = false;
        updBar.dataset.remote = d.remote;
      } else {
        updBar.hidden = true;
      }
    } catch (e) { /* 離線就不顯示 */ }
  }
  if (updBar) {
    updBtn.addEventListener("click", async () => {
      if (!confirm("要更新到最新版嗎？更新後程式會自動重開（打怪會中斷一下）。錢包和設定不會被動到。")) return;
      updBtn.disabled = true;
      updText.textContent = "更新中，請稍候…";
      try {
        await api("/api/update/apply", {});
        updText.textContent = "更新完成，程式重開中，約 15 秒後自動重新整理…";
        setTimeout(() => location.reload(), 15000);
      } catch (e) {
        updText.textContent = e.message;
        updBtn.disabled = false;
      }
    });
    document.getElementById("update-later").addEventListener("click", () => {
      updSkip = updBar.dataset.remote || "";
      updBar.hidden = true;
    });
    checkUpdate();
    setInterval(checkUpdate, 30 * 60 * 1000);
  }

  refresh();
  setInterval(refresh, 1500);
})();
