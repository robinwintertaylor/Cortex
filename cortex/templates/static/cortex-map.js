/* Semantic map renderer.
 *
 * Canvas 2D on purpose: a few thousand points is nothing to draw directly, and
 * it keeps the page dependency-free like the rest of the dashboard. Positions
 * come precomputed from the server (cortex/mapproj.py) — this file only draws
 * and navigates, it never lays anything out.
 */
(function () {
  "use strict";

  var el = document.getElementById("mapdata");
  if (!el) return;
  var DATA = JSON.parse(el.textContent);
  var PTS = DATA.points || [];
  var CLUSTERS = DATA.clusters || [];
  var LINKS = DATA.links || [];
  if (!PTS.length) return;

  var TYPE_COLOR = {};
  (DATA.types || []).forEach(function (t) { TYPE_COLOR[t.name] = t.color; });
  function colorOf(p) { return TYPE_COLOR[p.etype] || "#f7768e"; }

  var byId = {};
  PTS.forEach(function (p) {
    byId[p.id] = p;
    p.r = 4.2 + Math.sqrt(p.facts + p.doclinks) * 1.45;
  });

  // adjacency drives both the link overlay and the detail panel's neighbour list
  var adj = {};
  LINKS.forEach(function (l) {
    (adj[l.source] = adj[l.source] || []).push({ id: l.target, score: l.score, method: l.method });
    (adj[l.target] = adj[l.target] || []).push({ id: l.source, score: l.score, method: l.method });
  });

  // biggest first: label placement is first-come-first-served, and the regions
  // worth naming at a glance are the big ones
  var labelOrder = CLUSTERS.slice().sort(function (a, b) { return b.size - a.size; });

  var clusterMembers = {};
  CLUSTERS.forEach(function (c) {
    var mem = PTS.filter(function (p) { return p.cluster === c.id; });
    clusterMembers[c.id] = mem;
    var tally = {};
    mem.forEach(function (p) { tally[colorOf(p)] = (tally[colorOf(p)] || 0) + 1; });
    c.color = Object.keys(tally).sort(function (a, b) { return tally[b] - tally[a]; })[0] || "#565f73";
  });

  // ── state
  var types = (DATA.types || []).map(function (t) { return t.name; });
  var active = {};
  types.forEach(function (t) { active[t] = true; });
  var linkMode = "selected";
  var floor = 0.55;
  var hoverId = null, selectedId = null, query = "";
  var cam = { k: 1, tx: 0, ty: 0 };

  var canvas = document.getElementById("map");
  var ctx = canvas.getContext("2d");
  var tooltip = document.getElementById("tooltip");
  var W = 0, H = 0;

  function visible(p) { return !!active[p.etype]; }
  function matches(p) { return query && p.label.toLowerCase().indexOf(query) !== -1; }
  function sx(p) { return p.x * cam.k + cam.tx; }
  function sy(p) { return p.y * cam.k + cam.ty; }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function resize() {
    var rect = canvas.getBoundingClientRect();
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = rect.width; H = rect.height;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function fit() {
    var pts = PTS.filter(visible);
    if (!pts.length) pts = PTS;
    var xs = pts.map(function (p) { return p.x; }), ys = pts.map(function (p) { return p.y; });
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    var pad = 46;
    cam.k = Math.min((W - pad * 2) / Math.max(1, x1 - x0), (H - pad * 2) / Math.max(1, y1 - y0));
    cam.tx = W / 2 - (x0 + x1) / 2 * cam.k;
    cam.ty = H / 2 - (y0 + y1) / 2 * cam.k;
    draw();
  }

  function activeLinks() {
    if (linkMode === "off") return [];
    var base = LINKS.filter(function (l) {
      return l.score >= floor && byId[l.source] && byId[l.target]
        && visible(byId[l.source]) && visible(byId[l.target]);
    });
    if (linkMode === "all") return base;
    if (!selectedId) return [];
    return base.filter(function (l) { return l.source === selectedId || l.target === selectedId; });
  }

  function pointScale() { return Math.max(0.78, Math.min(1.7, cam.k * 0.95)); }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#12151b";
    ctx.fillRect(0, 0, W, H);

    // 1 — one soft wash per semantic region
    ctx.globalCompositeOperation = "lighter";
    CLUSTERS.forEach(function (c) {
      var mem = clusterMembers[c.id] || [];
      if (!mem.some(visible)) return;
      var x = c.x * cam.k + cam.tx, y = c.y * cam.k + cam.ty, r = c.radius * cam.k;
      if (x + r < 0 || x - r > W || y + r < 0 || y - r > H) return;
      var g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, c.color + "30");
      g.addColorStop(0.55, c.color + "14");
      g.addColorStop(1, c.color + "00");
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    });
    ctx.globalCompositeOperation = "source-over";

    // 2 — links
    var links = activeLinks();
    if (links.length) {
      ctx.lineWidth = Math.min(1.1, 0.5 + cam.k * 0.35);
      links.forEach(function (l) {
        var a = byId[l.source], b = byId[l.target];
        var strong = linkMode === "selected" || l.score >= 0.8;
        ctx.strokeStyle = "rgba(65,166,181," + (strong ? 0.5 : 0.13) + ")";
        ctx.beginPath();
        ctx.moveTo(sx(a), sy(a)); ctx.lineTo(sx(b), sy(b)); ctx.stroke();
      });
    }

    // 3 — region labels. Thirty-odd regions in one dense field will collide
    // into unreadable mush, so: biggest regions get first claim on the space,
    // and any label whose box would overlap one already placed is dropped
    // rather than drawn on top. Zooming in makes room and they come back.
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    var font = getComputedStyle(document.body).fontFamily;
    ctx.font = "600 9.5px " + font;
    ctx.fillStyle = "rgba(139,147,165,.78)";
    var placed = [];
    labelOrder.forEach(function (c) {
      var mem = clusterMembers[c.id] || [];
      if (!mem.some(visible) || c.size * cam.k < 1.1) return;
      var y = c.y * cam.k + cam.ty - c.radius * cam.k - 6;
      if (y < 4 || y > H - 4) return;
      var text = c.name.toUpperCase();
      var halfW = ctx.measureText(text).width / 2 + 5;
      // keep the label on screen instead of letting it clip at the edge
      var x = Math.min(Math.max(c.x * cam.k + cam.tx, halfW + 2), W - halfW - 2);
      if (x - halfW < -40 || x + halfW > W + 40) return;
      var box = { x0: x - halfW, x1: x + halfW, y0: y - 7, y1: y + 7 };
      for (var i = 0; i < placed.length; i++) {
        var o = placed[i];
        if (box.x0 < o.x1 && box.x1 > o.x0 && box.y0 < o.y1 && box.y1 > o.y0) return;
      }
      placed.push(box);
      ctx.fillText(text, x, y);
    });

    // 4 — points
    var dim = query !== "";
    var scale = pointScale();
    var shown = 0;
    PTS.forEach(function (p) {
      if (!visible(p)) return;
      shown++;
      var x = sx(p), y = sy(p);
      if (x < -40 || x > W + 40 || y < -40 || y > H + 40) return;
      var hot = p.id === hoverId || p.id === selectedId;
      var hit = dim && matches(p);
      var r = (hot ? p.r * 1.5 : p.r) * scale;
      var col = colorOf(p);

      ctx.globalAlpha = dim ? (hit ? 1 : 0.16) : 1;
      if (hot || hit) { ctx.shadowColor = col; ctx.shadowBlur = 16; }
      ctx.beginPath();
      if (p.ptype === "doc") {
        var s = r * 0.92;
        if (ctx.roundRect) ctx.roundRect(x - s, y - s, s * 2, s * 2, Math.max(1.5, s * 0.28));
        else ctx.rect(x - s, y - s, s * 2, s * 2);
      } else {
        ctx.arc(x, y, r, 0, Math.PI * 2);
      }
      var g = ctx.createRadialGradient(x - r * 0.35, y - r * 0.35, 0.5, x, y, r * 1.15);
      g.addColorStop(0, col + "ff");
      g.addColorStop(1, col + "b4");
      ctx.fillStyle = g; ctx.fill();
      ctx.shadowBlur = 0;
      ctx.lineWidth = p.id === selectedId ? 2 : 1;
      ctx.strokeStyle = p.id === selectedId ? "#e6e6e6" : "#0f1115";
      ctx.stroke();

      var showLabel = hot || hit || (cam.k > 0.42 && p.facts + p.doclinks >= 6) || cam.k > 0.9;
      if (showLabel) {
        var fs = hot ? 12 : 10.5;
        ctx.font = (hot ? "600 " : "") + fs + "px " + font;
        ctx.textBaseline = "top";
        var label = p.label.length > 34 ? p.label.slice(0, 32) + "…" : p.label;
        if (hot) {
          var tw = ctx.measureText(label).width;
          ctx.fillStyle = "rgba(15,17,21,.88)";
          ctx.fillRect(x - tw / 2 - 5, y + r + 3, tw + 10, fs + 6);
        }
        ctx.fillStyle = hot ? "#e6e6e6" : "rgba(230,230,230,.66)";
        ctx.fillText(label, x, y + r + 6);
      }
      ctx.globalAlpha = 1;
    });
    document.getElementById("p-shown").textContent = shown;
  }

  function pick(mx, my) {
    var best = null, bestD = 16, scale = pointScale();
    PTS.forEach(function (p) {
      if (!visible(p)) return;
      var d = Math.sqrt(Math.pow(sx(p) - mx, 2) + Math.pow(sy(p) - my, 2));
      var rr = Math.max(8, p.r * scale);
      if (d < rr + 4 && d < bestD + rr) { best = p; bestD = d; }
    });
    return best;
  }

  // ── interaction
  var dragging = false, moved = false, lastX = 0, lastY = 0;
  canvas.addEventListener("mousedown", function (e) {
    dragging = true; moved = false; lastX = e.clientX; lastY = e.clientY;
    canvas.style.cursor = "grabbing";
  });
  window.addEventListener("mouseup", function () {
    dragging = false; canvas.style.cursor = "grab";
  });
  canvas.style.cursor = "grab";
  canvas.addEventListener("mousemove", function (e) {
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left, my = e.clientY - rect.top;
    if (dragging) {
      if (Math.abs(e.clientX - lastX) + Math.abs(e.clientY - lastY) > 2) moved = true;
      cam.tx += e.clientX - lastX; cam.ty += e.clientY - lastY;
      lastX = e.clientX; lastY = e.clientY;
      tooltip.style.display = "none";
      draw();
      return;
    }
    var hit = pick(mx, my);
    var id = hit ? hit.id : null;
    if (id !== hoverId) { hoverId = id; draw(); }
    if (hit) {
      tooltip.style.display = "block";
      tooltip.innerHTML = "<div>" + esc(hit.label) + "</div><div class='sub'>" +
        esc(hit.etype) + (hit.project ? " · " + esc(hit.project) : "") +
        " · " + hit.facts + " facts · " + hit.doclinks + " links</div>";
      tooltip.style.left = Math.min(Math.max(6, mx + 14), W - tooltip.offsetWidth - 6) + "px";
      tooltip.style.top = Math.min(Math.max(6, my + 14), H - tooltip.offsetHeight - 6) + "px";
    } else {
      tooltip.style.display = "none";
    }
  });
  canvas.addEventListener("mouseleave", function () {
    tooltip.style.display = "none";
    if (hoverId) { hoverId = null; draw(); }
  });
  canvas.addEventListener("click", function (e) {
    if (moved) return;
    var rect = canvas.getBoundingClientRect();
    var hit = pick(e.clientX - rect.left, e.clientY - rect.top);
    select(hit ? hit.id : null);
  });
  canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left, my = e.clientY - rect.top;
    var k2 = Math.max(0.12, Math.min(9, cam.k * Math.exp(-e.deltaY * 0.0016)));
    var s = k2 / cam.k;
    cam.tx = mx - (mx - cam.tx) * s;
    cam.ty = my - (my - cam.ty) * s;
    cam.k = k2;
    draw();
  }, { passive: false });

  function zoomBy(f) {
    var k2 = Math.max(0.12, Math.min(9, cam.k * f));
    var s = k2 / cam.k;
    cam.tx = W / 2 - (W / 2 - cam.tx) * s;
    cam.ty = H / 2 - (H / 2 - cam.ty) * s;
    cam.k = k2; draw();
  }
  document.getElementById("z-in").onclick = function () { zoomBy(1.35); };
  document.getElementById("z-out").onclick = function () { zoomBy(1 / 1.35); };
  document.getElementById("z-fit").onclick = fit;

  // ── detail panel
  function select(id) {
    selectedId = id;
    var body = document.getElementById("detail-body");
    if (!id || !byId[id]) {
      body.innerHTML = "<p class='small dimmer'>Click any point to inspect it — its "
        + "type, its project, the facts recorded against it, and every document "
        + "linked above the current score floor.</p>";
      draw();
      return;
    }
    var p = byId[id];
    var nbrs = (adj[id] || []).filter(function (n) {
      return n.score >= floor && byId[n.id];
    }).sort(function (a, b) { return b.score - a.score; });

    var href = p.ptype === "doc" ? "/documents" : "/entity/" + encodeURIComponent(p.label);
    var html = "<h3 class='d-name'><a href='" + href + "'>" + esc(p.label) + "</a></h3>"
      + "<div class='row' style='gap:5px'><span class='tag'>" + esc(p.etype) + "</span>"
      + (p.project ? "<span class='tag plain'>" + esc(p.project) + "</span>" : "")
      + "<span class='tag plain'>" + (p.ptype === "doc" ? "document" : "entity") + "</span></div>"
      + "<div class='grid cols-2' style='margin-top:12px'>"
      + "<div class='stat'><div class='k'>facts</div><div class='v'>" + p.facts + "</div></div>"
      + "<div class='stat'><div class='k'>links kept</div><div class='v'>" + nbrs.length + "</div></div>"
      + "</div>";
    if (p.summary && p.summary.trim()) {
      html += "<p class='quote' style='margin-top:12px'>" + esc(p.summary.trim()) + "</p>";
    }
    html += "<p class='label' style='margin-top:16px'>Linked "
      + (p.ptype === "doc" ? "entities" : "documents") + " · " + nbrs.length + "</p>";
    if (!nbrs.length) {
      html += "<p class='small dimmer'>Nothing above the " + floor.toFixed(2) + " floor.</p>";
    } else {
      html += "<div class='nbr'>";
      nbrs.slice(0, 40).forEach(function (n) {
        var q = byId[n.id];
        html += "<button type='button' data-goto='" + esc(q.id) + "'>"
          + "<span class='dot' style='background:" + colorOf(q) + "'></span>"
          + "<span class='lbl'>" + esc(q.label) + "</span>"
          + "<span class='sc'>" + n.score.toFixed(2) + "</span></button>";
      });
      html += "</div>";
    }
    body.innerHTML = html;
    Array.prototype.forEach.call(body.querySelectorAll("[data-goto]"), function (b) {
      b.onclick = function () {
        var t = byId[b.getAttribute("data-goto")];
        if (!t) return;
        cam.tx = W / 2 - t.x * cam.k;
        cam.ty = H / 2 - t.y * cam.k;
        select(t.id);
      };
    });
    draw();
  }

  // ── type filter chips
  var chipWrap = document.getElementById("type-chips");
  (DATA.types || []).forEach(function (t) {
    var b = document.createElement("button");
    b.type = "button"; b.className = "chip";
    b.setAttribute("aria-pressed", "true");
    b.style.color = t.color;
    var isDoc = ["note", "document", "research", "summary"].indexOf(t.name) !== -1;
    b.innerHTML = "<span class='" + (isDoc ? "sq" : "dot") + "' style='background:"
      + t.color + "'></span>" + esc(t.name) + " <span class='n'>" + t.count + "</span>";
    b.onclick = function () {
      active[t.name] = !active[t.name];
      b.setAttribute("aria-pressed", String(active[t.name]));
      if (selectedId && byId[selectedId] && !visible(byId[selectedId])) select(null);
      draw();
    };
    chipWrap.appendChild(b);
  });

  // ── link mode
  Array.prototype.forEach.call(document.querySelectorAll("#link-mode button"), function (b) {
    b.onclick = function () {
      linkMode = b.getAttribute("data-mode");
      Array.prototype.forEach.call(document.querySelectorAll("#link-mode button"), function (o) {
        o.setAttribute("aria-pressed", String(o === b));
      });
      draw();
    };
  });

  // ── score floor + its histogram
  var hist = document.getElementById("hist"), hctx = hist.getContext("2d");
  var BINS = 16, LO = 0.5, HI = 0.95;
  var counts = new Array(BINS);
  for (var i = 0; i < BINS; i++) counts[i] = 0;
  LINKS.forEach(function (l) {
    var b = Math.floor((l.score - LO) / (HI - LO) * BINS);
    counts[Math.min(BINS - 1, Math.max(0, b))]++;
  });
  var peak = Math.max.apply(null, counts) || 1;

  function drawHist() {
    var w = hist.width, h = hist.height, base = h - 16, bw = w / BINS;
    hctx.clearRect(0, 0, w, h);
    for (var i = 0; i < BINS; i++) {
      var binLo = LO + (i / BINS) * (HI - LO);
      var bh = counts[i] / peak * (base - 6);
      hctx.fillStyle = binLo + 1e-9 >= floor ? "#9ece6a" : "#3a4254";
      hctx.fillRect(i * bw + 1, base - bh, bw - 2, bh);
    }
    var fx = (floor - LO) / (HI - LO) * w;
    hctx.strokeStyle = "#e0af68"; hctx.lineWidth = 2;
    hctx.beginPath(); hctx.moveTo(fx, 0); hctx.lineTo(fx, base); hctx.stroke();
    hctx.fillStyle = "#6b7385";
    hctx.font = "16px ui-monospace, monospace";
    hctx.textBaseline = "alphabetic";
    hctx.textAlign = "left"; hctx.fillText(LO.toFixed(2), 2, h - 2);
    hctx.textAlign = "right"; hctx.fillText(HI.toFixed(2), w - 2, h - 2);
  }

  function applyFloor() {
    var kept = LINKS.filter(function (l) { return l.score >= floor; });
    var perDoc = {};
    kept.forEach(function (l) { perDoc[l.source] = (perDoc[l.source] || 0) + 1; });
    var vals = Object.keys(perDoc).map(function (k) { return perDoc[k]; });
    var docs = DATA.stats.docs || 1;
    document.getElementById("t-val").textContent = floor.toFixed(2);
    document.getElementById("r-kept").textContent = kept.length;
    document.getElementById("r-avg").textContent = (kept.length / docs).toFixed(1);
    document.getElementById("r-max").textContent = vals.length ? Math.max.apply(null, vals) : 0;
    document.getElementById("r-cut").textContent = LINKS.length - kept.length;
    drawHist();
    if (selectedId) select(selectedId); else draw();
  }
  document.getElementById("t-range").addEventListener("input", function (e) {
    floor = parseFloat(e.target.value);
    applyFloor();
  });

  // ── search within the map
  var qStatus = document.getElementById("q-status");
  var defaultStatus = qStatus.textContent;
  document.getElementById("q").addEventListener("input", function (e) {
    query = e.target.value.trim().toLowerCase();
    if (!query) {
      qStatus.textContent = defaultStatus;
    } else {
      var hits = PTS.filter(function (p) { return visible(p) && matches(p); });
      qStatus.textContent = hits.length
        ? hits.length + " match" + (hits.length === 1 ? "" : "es") + " highlighted."
        : "No match on the map.";
      if (hits.length === 1) select(hits[0].id);
    }
    draw();
  });

  if (window.ResizeObserver) new ResizeObserver(resize).observe(canvas);
  else window.addEventListener("resize", resize);
  resize(); fit(); applyFloor();
})();
