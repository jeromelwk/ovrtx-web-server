"use strict";

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || data.error || `${res.status} ${res.statusText}`);
  }
  return data;
}

function setStatus(text, kind) {
  const el = $("statusBadge");
  el.textContent = text;
  el.className = kind ? kind : "";
}

// ---------------------------------------------------------------------------
// Native Windows "Open File" / "Select Folder" dialogs, plus the small modal
// used only to disambiguate when a folder contains several USD candidates.
// ---------------------------------------------------------------------------
function closeModal() {
  $("modalOverlay").hidden = true;
}

$("btnModalClose").addEventListener("click", closeModal);
$("btnModalCancel").addEventListener("click", closeModal);
$("modalOverlay").addEventListener("click", (e) => {
  if (e.target.id === "modalOverlay") closeModal();
});

// ---------------------------------------------------------------------------
// Hamburger menu (open file / open folder)
// ---------------------------------------------------------------------------
function closeMenu() {
  $("menuDropdown").hidden = true;
  $("btnMenu").setAttribute("aria-expanded", "false");
}
function toggleMenu() {
  const open = $("menuDropdown").hidden;
  $("menuDropdown").hidden = !open;
  $("btnMenu").setAttribute("aria-expanded", String(open));
}

$("btnMenu").addEventListener("click", (e) => {
  e.stopPropagation();
  toggleMenu();
});
document.addEventListener("click", (e) => {
  if (!$("menuDropdown").hidden && !e.target.closest(".menu-wrap")) closeMenu();
});

$("btnOpenFile").addEventListener("click", async () => {
  closeMenu();
  try {
    const data = await api("/api/dialog/file", { method: "POST" });
    if (data.path) openScene(data.path);
  } catch (err) {
    setStatus("erreur", "error");
    console.error(err);
  }
});
$("btnOpenFolder").addEventListener("click", async () => {
  closeMenu();
  try {
    const data = await api("/api/dialog/folder", { method: "POST" });
    if (data.path) openScene(data.path);
  } catch (err) {
    setStatus("erreur", "error");
    console.error(err);
  }
});

$("btnSettings").addEventListener("click", () => {
  closeMenu();
  HomeAssistant.open();
});

// ---------------------------------------------------------------------------
// Animation playback (play / pause / stop / timeline scrub)
// ---------------------------------------------------------------------------
const Anim = {
  scrubbing: false,
  unsafe: false,
  playing: false,
  loop: true,

  init() {
    $("btnAnimPlayPause").addEventListener("click", () => {
      if (this.playing) {
        Viewport.send({ type: "anim_pause" });
      } else if (!this.unsafe) {
        Viewport.send({ type: "anim_play" });
      }
    });
    $("btnAnimStop").addEventListener("click", () => Viewport.send({ type: "anim_stop" }));
    $("btnAnimLoop").addEventListener("click", () => {
      Viewport.send({ type: "anim_loop", value: !this.loop });
    });

    const slider = $("animSlider");
    slider.addEventListener("pointerdown", () => { this.scrubbing = true; });
    slider.addEventListener("input", () => {
      if (this.unsafe) return;
      $("animTime").textContent = Number(slider.value).toFixed(1) + " s";
      Viewport.send({ type: "anim_seek", time: Number(slider.value) });
    });
    const release = () => { this.scrubbing = false; };
    slider.addEventListener("pointerup", release);
    slider.addEventListener("pointercancel", release);
  },

  setUnsafe(unsafe) {
    this.unsafe = unsafe;
    $("animUnsafe").hidden = !unsafe;
    $("btnAnimPlayPause").disabled = unsafe;
    $("animSlider").disabled = unsafe;
  },

  update(state) {
    if (state.unsafe !== undefined) this.setUnsafe(state.unsafe);
    const start = state.start || 0;
    if (!this.scrubbing) {
      $("animSlider").min = start;
      $("animSlider").max = start + state.duration;
      $("animSlider").value = state.time;
      $("animTime").textContent = state.time.toFixed(1) + " s";
    }
    $("animDuration").textContent = state.duration.toFixed(1) + " s";
    this.playing = state.playing;
    const btn = $("btnAnimPlayPause");
    btn.textContent = state.playing ? "⏸" : "▶";
    btn.title = state.playing ? "Pause" : "Lecture";
    btn.classList.toggle("active", state.playing);

    if (state.loop !== undefined) {
      this.loop = state.loop;
      $("btnAnimLoop").classList.toggle("active", state.loop);
    }
  },
};
Anim.init();

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------------------------------------------------------------------------
// Opening a scene
// ---------------------------------------------------------------------------
async function openScene(path, chosen) {
  setStatus("ouverture…", "busy");
  try {
    const body = { path };
    if (chosen) body.chosen = chosen;
    const rect = $("viewportContainer").getBoundingClientRect();
    body.width = Math.max(64, Math.round(rect.width));
    body.height = Math.max(64, Math.round(rect.height));
    const data = await api("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (data.need_choice) {
      showCandidateChooser(path, data.candidates);
      setStatus("choix requis", "");
      return;
    }

    $("currentPath").textContent = data.opened_path;
    $("currentPath").title = data.opened_path;
    $("viewportOverlay").hidden = true;
    $("viewportHint").hidden = false;
    Anim.setUnsafe(!!data.anim_unsafe);
    setStatus("prêt", "ok");
    await loadTreeRoot();
    Viewport.connect();
  } catch (err) {
    setStatus("erreur", "error");
    $("viewportOverlay").textContent = "Erreur à l'ouverture : " + err.message;
    $("viewportOverlay").hidden = false;
  }
}

function showCandidateChooser(folderPath, candidates) {
  const list = $("browserList");
  $("modalTitle").textContent = "Plusieurs fichiers USD trouvés — choisissez-en un";
  $("modalCurrentPath").textContent = folderPath;
  $("modalOverlay").hidden = false;
  list.innerHTML = "";
  for (const entry of candidates) {
    const row = document.createElement("div");
    row.className = "browser-row usd";
    row.innerHTML = `<span class="icon">📄</span><span class="nm">${escapeHtml(entry.name)}</span>`;
    row.addEventListener("click", () => {
      closeModal();
      openScene(folderPath, entry.path);
    });
    list.appendChild(row);
  }
}

// ---------------------------------------------------------------------------
// Scene graph tree (left pane)
// ---------------------------------------------------------------------------
const Tree = {
  selectedPath: null,

  async loadRoot() {
    const container = $("treeContainer");
    container.innerHTML = "";
    const roots = await api("/api/tree/children?path=" + encodeURIComponent("/"));
    if (!roots.length) {
      container.innerHTML = '<div class="tree-empty">Scène vide.</div>';
      return;
    }
    for (const node of roots) {
      container.appendChild(this.buildNode(node));
    }
  },

  buildNode(node) {
    const wrap = document.createElement("div");
    wrap.className = "tree-node";

    const row = document.createElement("div");
    row.className = "tree-row";
    row.dataset.path = node.path;

    const toggle = document.createElement("span");
    toggle.className = "tree-toggle" + (node.has_children ? "" : " leaf");
    toggle.textContent = node.has_children ? "▶" : "";
    row.appendChild(toggle);

    const icon = document.createElement("span");
    icon.className = "tree-icon";
    icon.textContent = iconFor(node.type_name);
    row.appendChild(icon);

    const name = document.createElement("span");
    name.className = "tree-name";
    name.textContent = node.name;
    row.appendChild(name);

    const type = document.createElement("span");
    type.className = "tree-type";
    type.textContent = node.type_name || "";
    row.appendChild(type);

    wrap.appendChild(row);

    const childrenBox = document.createElement("div");
    childrenBox.className = "tree-children collapsed";
    wrap.appendChild(childrenBox);

    let loaded = false;

    const expand = async () => {
      if (!node.has_children) return;
      if (!loaded) {
        const kids = await api("/api/tree/children?path=" + encodeURIComponent(node.path));
        for (const kid of kids) childrenBox.appendChild(this.buildNode(kid));
        loaded = true;
      }
      const collapsed = childrenBox.classList.toggle("collapsed");
      toggle.textContent = collapsed ? "▶" : "▼";
    };

    toggle.addEventListener("click", (e) => { e.stopPropagation(); expand(); });
    row.addEventListener("click", () => {
      this.select(node.path);
      if (!node.has_children) return;
    });

    return wrap;
  },

  select(path) {
    this.selectedPath = path;
    document.querySelectorAll(".tree-row.selected").forEach((el) => el.classList.remove("selected"));
    const row = document.querySelector(`.tree-row[data-path="${cssEscape(path)}"]`);
    if (row) {
      row.classList.add("selected");
      row.scrollIntoView({ block: "nearest" });
    }
    Properties.load(path);
    Viewport.selectPrim(path);
  },

  selectFromExternal(path) {
    // Called when the viewport reports a pick; expand ancestors is skipped
    // for simplicity (path is still shown correctly in the properties panel).
    this.select(path);
  },

  deselect() {
    if (!this.selectedPath) return;
    this.selectedPath = null;
    document.querySelectorAll(".tree-row.selected").forEach((el) => el.classList.remove("selected"));
    $("propsContainer").innerHTML = '<div class="prop-empty">Sélectionnez un prim dans le viewer ou le graphe de scène.</div>';
    Viewport.selectPrim(null);
  },
};

function cssEscape(s) {
  return s.replace(/["\\]/g, "\\$&");
}

function iconFor(typeName) {
  switch (typeName) {
    case "Mesh": return "◆";
    case "Xform": return "⊞";
    case "Camera": return "🎥";
    case "DomeLight":
    case "DistantLight":
    case "SphereLight":
    case "RectLight": return "💡";
    case "Scope": return "▤";
    case "PointInstancer": return "⋮⋮";
    case "Material": return "🎨";
    default: return "•";
  }
}

async function loadTreeRoot() { await Tree.loadRoot(); }

// ---------------------------------------------------------------------------
// Properties panel (right pane)
// ---------------------------------------------------------------------------
const Properties = {
  async load(path) {
    const container = $("propsContainer");
    container.innerHTML = '<div class="prop-empty">Chargement…</div>';
    try {
      const info = await api("/api/prim?path=" + encodeURIComponent(path));
      this.render(info);
    } catch (err) {
      container.innerHTML = `<div class="prop-empty">Erreur : ${escapeHtml(err.message)}</div>`;
    }
  },

  render(info) {
    const container = $("propsContainer");
    container.innerHTML = "";

    const pathEl = document.createElement("div");
    pathEl.className = "prop-prim-path";
    pathEl.textContent = info.path;
    container.appendChild(pathEl);

    container.appendChild(this.group("Métadonnées", Object.entries(info.metadata || {}).map(
      ([k, v]) => ({ k, v: fmt(v) })
    )));

    if (info.attributes && info.attributes.length) {
      const rows = info.attributes.map((a) => ({ k: a.name, v: fmt(a.value), t: a.type_name }));
      container.appendChild(this.group("Attributs", rows, true));
    }
  },

  group(title, rows, withType) {
    const box = document.createElement("div");
    box.className = "prop-group";
    const h = document.createElement("div");
    h.className = "prop-group-title";
    h.textContent = title;
    box.appendChild(h);

    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "prop-empty";
      empty.style.padding = "4px";
      empty.textContent = "—";
      box.appendChild(empty);
      return box;
    }

    const table = document.createElement("table");
    table.className = "prop-table";
    for (const row of rows) {
      const tr = document.createElement("tr");
      const tdK = document.createElement("td");
      tdK.className = "k";
      tdK.textContent = row.k;
      const tdV = document.createElement("td");
      tdV.className = "v";
      tdV.textContent = row.v;
      tr.appendChild(tdK);
      tr.appendChild(tdV);
      if (withType) {
        const tdT = document.createElement("td");
        tdT.className = "t";
        tdT.textContent = row.t || "";
        tr.appendChild(tdT);
      }
      table.appendChild(tr);
    }
    box.appendChild(table);
    return box;
  },
};

function fmt(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

// ---------------------------------------------------------------------------
// Home Assistant MQTT bridge panel (opened from the toolbar's gear icon)
// ---------------------------------------------------------------------------
const HomeAssistant = {
  statusTimer: null,

  init() {
    $("btnHaClose").addEventListener("click", () => this.close());
    $("haOverlay").addEventListener("click", (e) => { if (e.target.id === "haOverlay") this.close(); });

    $("btnHaConnect").addEventListener("click", () => this.onConnectClicked());
    $("btnHaSaveConn").addEventListener("click", () => this.saveConnection());
    $("btnHaDetect").addEventListener("click", () => this.detect());
    $("btnHaUseSelection").addEventListener("click", () => {
      if (Tree.selectedPath) $("haPrimPath").value = Tree.selectedPath;
    });
    $("btnHaAddMapping").addEventListener("click", () => this.addMapping());
    $("btnHaLoad").addEventListener("click", () => this.loadMappings());
    $("btnHaSave").addEventListener("click", () => this.saveMappings());
  },

  async open() {
    $("haOverlay").hidden = false;
    await this.loadConnection();
    await this.refreshStatus();
    await this.refreshMappings();
    if (!this.statusTimer) {
      this.statusTimer = setInterval(() => this.refreshStatus(), 3000);
    }
  },

  close() {
    $("haOverlay").hidden = true;
    if (this.statusTimer) {
      clearInterval(this.statusTimer);
      this.statusTimer = null;
    }
  },

  async loadConnection() {
    try {
      const c = await api("/api/ha/connection");
      $("haHost").value = c.host || "";
      $("haPort").value = c.port || 1883;
      $("haUser").value = c.username || "";
      $("haPassword").value = c.password || "";
      $("haTls").checked = !!c.tls_enabled;
      $("haTopicFilter").value = c.topic_filter || "#";
      $("haQos").value = String(c.qos || 0);
    } catch (err) {
      console.error(err);
    }
  },

  readConnection() {
    return {
      host: $("haHost").value.trim(),
      port: parseInt($("haPort").value, 10) || 1883,
      username: $("haUser").value.trim(),
      password: $("haPassword").value,
      tls_enabled: $("haTls").checked,
      topic_filter: $("haTopicFilter").value.trim() || "#",
      qos: parseInt($("haQos").value, 10) || 0,
    };
  },

  async saveConnection() {
    try {
      await api("/api/ha/connection", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(this.readConnection()),
      });
    } catch (err) {
      alert("Erreur : " + err.message);
    }
  },

  async onConnectClicked() {
    const status = await api("/api/ha/status").catch(() => ({ connected: false }));
    if (status.connected) {
      await api("/api/ha/disconnect", { method: "POST" });
      await this.refreshStatus();
      return;
    }
    $("btnHaConnect").disabled = true;
    try {
      await api("/api/ha/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(this.readConnection()),
      });
    } catch (err) {
      alert("Connexion échouée : " + err.message);
    } finally {
      $("btnHaConnect").disabled = false;
      await this.refreshStatus();
    }
  },

  async refreshStatus() {
    try {
      const s = await api("/api/ha/status");
      $("haStatusDot").classList.toggle("connected", !!s.connected);
      $("haStatusText").textContent = s.connected ? "Connecté" : "Déconnecté";
      $("btnHaConnect").textContent = s.connected ? "Se déconnecter" : "Se connecter";
    } catch (err) {
      console.error(err);
    }
  },

  async detect() {
    try {
      const data = await api("/api/ha/mappings/detect", { method: "POST" });
      this.renderMappings(data.mappings);
    } catch (err) {
      alert("Erreur : " + err.message);
    }
  },

  async addMapping() {
    const kind = $("haKind").value;
    const prim_path = $("haPrimPath").value.trim();
    const topic_prefix = $("haTopicPrefix").value.trim().replace(/\/+$/, "");
    const max_intensity = parseFloat($("haMaxIntensity").value) || 0;
    if (!prim_path || !topic_prefix) {
      alert("Le prim cible et le topic de base sont requis.");
      return;
    }
    try {
      await api("/api/ha/mappings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prim_path, topic_prefix, kind, max_intensity, max_exposure: 0 }),
      });
      await this.refreshMappings();
    } catch (err) {
      alert("Erreur : " + err.message);
    }
  },

  async removeMapping(topic_prefix) {
    try {
      await api("/api/ha/mappings/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic_prefix }),
      });
      await this.refreshMappings();
    } catch (err) {
      alert("Erreur : " + err.message);
    }
  },

  async loadMappings() {
    try {
      const data = await api("/api/ha/mappings/load", { method: "POST" });
      this.renderMappings(data.mappings);
    } catch (err) {
      alert("Erreur : " + err.message);
    }
  },

  async saveMappings() {
    try {
      await api("/api/ha/mappings/save", { method: "POST" });
    } catch (err) {
      alert("Erreur : " + err.message);
    }
  },

  async refreshMappings() {
    try {
      const mappings = await api("/api/ha/mappings");
      this.renderMappings(mappings);
    } catch (err) {
      console.error(err);
    }
  },

  renderMappings(mappings) {
    const container = $("haMappingList");
    container.innerHTML = "";
    if (!mappings || !mappings.length) {
      container.innerHTML = '<div class="ha-mapping-empty">(aucun mapping)</div>';
      return;
    }
    for (const m of mappings) {
      const row = document.createElement("div");
      row.className = "ha-mapping-row";
      row.innerHTML = `
        <span class="ha-kind">${m.kind === "presence" ? "détecteur" : "lumière"}</span>
        <span class="ha-prim" title="${escapeHtml(m.prim_path)}">${escapeHtml(m.prim_path)}</span>
        <span class="ha-topic" title="${escapeHtml(m.topic_prefix)}">${escapeHtml(m.topic_prefix)}</span>
      `;
      const removeBtn = document.createElement("button");
      removeBtn.className = "ha-remove";
      removeBtn.textContent = "✕";
      removeBtn.addEventListener("click", () => this.removeMapping(m.topic_prefix));
      row.appendChild(removeBtn);
      container.appendChild(row);
    }
  },
};
HomeAssistant.init();

// ---------------------------------------------------------------------------
// Viewport (center pane): WebSocket-streamed RTX frames + camera controls
// ---------------------------------------------------------------------------
const Viewport = {
  ws: null,
  canvas: null,
  ctx: null,
  dragging: false,
  dragButton: 0,
  dragMode: null, // "orbit" | "pan" | "dolly" | "gizmo" (chosen at mousedown, "orbit" may later be upgraded to "gizmo")
  pendingDragPick: false,
  gizmoAxis: null,
  lastX: 0,
  lastY: 0,
  frameCount: 0,
  lastFpsTime: 0,

  init() {
    this.canvas = $("viewportCanvas");
    this.ctx = this.canvas.getContext("2d");

    const container = $("viewportContainer");
    const resize = () => {
      const rect = container.getBoundingClientRect();
      this.canvas.width = Math.max(64, Math.round(rect.width));
      this.canvas.height = Math.max(64, Math.round(rect.height));
    };
    new ResizeObserver(resize).observe(container);
    resize();

    this.canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    this.canvas.addEventListener("mousedown", (e) => {
      this.dragging = true;
      this.dragButton = e.button;
      this.lastX = e.clientX;
      this.lastY = e.clientY;

      if (e.button === 1 || e.shiftKey) {
        this.dragMode = "pan";
      } else if (e.button === 2) {
        this.dragMode = "dolly";
      } else {
        // Left button: default to orbiting immediately (feels responsive),
        // but probe with a pick — if it lands on a gizmo axis handle before
        // the drag has gone far, retroactively switch to a constrained
        // single-axis move instead (see onMessage's "pick_result" handling).
        this.dragMode = "orbit";
        this.pendingDragPick = true;
        const rect = this.canvas.getBoundingClientRect();
        this.send({ type: "pick", x: (e.clientX - rect.left) / rect.width, y: (e.clientY - rect.top) / rect.height });
      }
    });
    window.addEventListener("mouseup", () => {
      if (this.dragging && this.dragMode === "gizmo") {
        this.send({ type: "gizmo_drag_end" });
        if (Tree.selectedPath) Properties.load(Tree.selectedPath);
      }
      this.dragging = false;
      this.dragMode = null;
      this.pendingDragPick = false;
      this.gizmoAxis = null;
      this.canvas.style.cursor = "grab";
    });
    window.addEventListener("mousemove", (e) => {
      if (!this.dragging) return;
      const dx = e.clientX - this.lastX;
      const dy = e.clientY - this.lastY;
      this.lastX = e.clientX;
      this.lastY = e.clientY;
      if (this.dragMode === "pan") {
        this.send({ type: "pan", dx, dy });
      } else if (this.dragMode === "dolly") {
        this.send({ type: "dolly", delta: dy });
      } else if (this.dragMode === "gizmo") {
        this.send({ type: "gizmo_drag", dx, dy });
      } else if (this.dragMode === "orbit") {
        this.send({ type: "orbit", dx, dy });
      }
    });
    this.canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.send({ type: "dolly", delta: e.deltaY * 0.3 });
    }, { passive: false });
  },

  connect() {
    if (this.ws) { try { this.ws.close(); } catch (_) {} }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/ws/viewport`);
    this.ws.binaryType = "arraybuffer";
    this.ws.addEventListener("open", () => {
      this.send({ type: "resize", width: this.canvas.width, height: this.canvas.height });
    });
    this.ws.addEventListener("message", (ev) => this.onMessage(ev));
    this.ws.addEventListener("close", () => { $("viewportHud").hidden = true; });
  },

  send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  },

  selectPrim(path) {
    this.send({ type: "select", path });
  },

  onMessage(ev) {
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.type === "pick_result") {
        if (this.pendingDragPick && this.dragging) {
          this.pendingDragPick = false;
          if (msg.axis) {
            this.dragMode = "gizmo";
            this.gizmoAxis = msg.axis;
            this.canvas.style.cursor = "move";
            this.send({ type: "gizmo_drag_start", axis: msg.axis });
          } else if (msg.path) {
            Tree.selectFromExternal(msg.path);
          }
        } else if (msg.path && !msg.axis) {
          Tree.selectFromExternal(msg.path);
        }
      } else if (msg.type === "stats") {
        const hud = $("viewportHud");
        hud.hidden = false;
        hud.textContent = `${msg.fps.toFixed(1)} fps · ${msg.width}×${msg.height}`;
      } else if (msg.type === "anim") {
        Anim.update(msg);
      } else if (msg.type === "error") {
        console.error("viewport error:", msg.message);
      }
      return;
    }
    // Binary frame: JPEG bytes -> draw to canvas.
    const blob = new Blob([ev.data], { type: "image/jpeg" });
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
      this.ctx.drawImage(img, 0, 0, this.canvas.width, this.canvas.height);
      URL.revokeObjectURL(url);
    };
    img.src = url;
  },
};

// ---------------------------------------------------------------------------
// Keyboard shortcuts: Escape clears the selection, F frames the selected prim.
// ---------------------------------------------------------------------------
window.addEventListener("keydown", (e) => {
  const tag = e.target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return;

  if (e.key === "Escape") {
    if (!$("haOverlay").hidden) {
      HomeAssistant.close();
    } else if (!$("modalOverlay").hidden) {
      closeModal();
    } else if (!$("menuDropdown").hidden) {
      closeMenu();
    } else {
      Tree.deselect();
    }
  } else if ((e.key === "f" || e.key === "F") && $("modalOverlay").hidden) {
    if (Tree.selectedPath) Viewport.send({ type: "frame_selected" });
  }
});

Viewport.init();

// ---------------------------------------------------------------------------
// Optional auto-open via ?open=<path> (used when launching the server
// straight into a given scene instead of requiring a manual File > Open).
// ---------------------------------------------------------------------------
const _autoOpenPath = new URLSearchParams(location.search).get("open");
if (_autoOpenPath) openScene(_autoOpenPath);
