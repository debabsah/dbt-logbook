/* dbt-logbook UI - vanilla JS, hash routing, no build step. */

const view = document.getElementById("view");
const tooltip = document.getElementById("tooltip");

const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtSecs = (s) => (s == null ? "–" : s < 10 ? s.toFixed(2) + "s" : s.toFixed(1) + "s");
const fmtTime = (t) => (t ? t.replace("T", " ").slice(0, 19) : "–");
const isFail = (s) => ["error", "fail", "runtime error"].includes(String(s).toLowerCase());
const statusHtml = (status) => {
  const bad = isFail(status) || status === "error";
  return `<span class="status ${bad ? "error" : "good"}"><span class="dot"></span>${bad ? "✗ " : "✓ "}${esc(status)}</span>`;
};

async function api(path) {
  const r = await fetch("/api" + path);
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  return r.json();
}

function showTip(ev, html) {
  tooltip.innerHTML = html;
  tooltip.style.display = "block";
  tooltip.style.left = Math.min(ev.clientX + 12, innerWidth - 220) + "px";
  tooltip.style.top = ev.clientY + 12 + "px";
}
const hideTip = () => (tooltip.style.display = "none");

/* ---------- runs timeline ---------- */

async function renderRuns() {
  const data = await api("/runs?limit=100");
  if (!data.total) {
    view.innerHTML = `<div class="card"><div class="empty">
      No runs recorded yet. Run <code>dbt-logbook import</code> to ingest existing
      artifacts, or wrap your runs: <code>dbt-logbook exec -- dbt build</code>.
      Want a populated playground? <code>dbt-logbook demo</code>.</div></div>`;
    return;
  }
  const strip = data.runs
    .slice()
    .reverse()
    .map(
      (r) =>
        `<a class="${r.status === "error" ? "error" : "good"}" href="#/run/${esc(r.invocation_id)}"
            title="${esc(fmtTime(r.generated_at))} ${esc(r.status)}">${r.status === "error" ? "✗" : "✓"}</a>`
    )
    .join("");
  const rows = data.runs
    .map(
      (r) => `<tr class="clickable" onclick="location.hash='#/run/${esc(r.invocation_id)}'">
      <td>${fmtTime(r.generated_at)}</td>
      <td>${statusHtml(r.status)}</td>
      <td>${esc(r.env)}</td>
      <td class="num">${r.nodes ?? 0}</td>
      <td class="num">${r.failed || 0}</td>
      <td class="num">${fmtSecs(r.elapsed)}</td>
      <td>${esc(r.dbt_version ?? "")}</td></tr>`
    )
    .join("");
  view.innerHTML = `
    <div class="card"><h2>Run history (newest last)</h2><div class="strip">${strip}</div></div>
    <div class="card"><h2>Runs</h2>
      <table><thead><tr><th>When (UTC)</th><th>Status</th><th>Env</th>
        <th style="text-align:right">Nodes</th><th style="text-align:right">Failed</th>
        <th style="text-align:right">Elapsed</th><th>dbt</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

/* ---------- run detail ---------- */

async function renderRun(id) {
  const r = await api("/runs/" + encodeURIComponent(id));
  const rows = r.results
    .map(
      (n) => `<tr class="clickable" onclick="location.hash='#/model/${esc(n.unique_id)}'">
      <td>${esc(n.unique_id)}</td><td>${statusHtml(n.status)}</td>
      <td class="num">${fmtSecs(n.execution_time)}</td>
      <td>${esc(n.message ?? "")}</td></tr>`
    )
    .join("");
  view.innerHTML = `
    <div class="card"><h2>Run ${esc(id)}</h2>
      <p>${fmtTime(r.generated_at)} · ${statusHtml(r.status)} · env <b>${esc(r.env)}</b>
         · dbt ${esc(r.dbt_version ?? "?")} · ${fmtSecs(r.elapsed)}</p>
      <table><thead><tr><th>Node</th><th>Status</th>
        <th style="text-align:right">Time</th><th>Message</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

/* ---------- model detail + sparkline ---------- */

function sparkline(history) {
  const W = 640, H = 120, PAD = 28;
  const times = history.map((h) => h.execution_time ?? 0);
  const max = Math.max(...times, 0.01);
  const x = (i) => PAD + (i * (W - 2 * PAD)) / Math.max(history.length - 1, 1);
  const y = (v) => H - PAD - (v / max) * (H - 2 * PAD);
  const path = history.map((h, i) => `${i ? "L" : "M"}${x(i)},${y(h.execution_time ?? 0)}`).join(" ");
  const marks = history
    .map((h, i) => {
      const cx = x(i), cy = y(h.execution_time ?? 0);
      // Failed runs: status color + a cross glyph, never color alone.
      return isFail(h.status)
        ? `<g class="pt" data-i="${i}"><line x1="${cx - 4}" y1="${cy - 4}" x2="${cx + 4}" y2="${cy + 4}" stroke="var(--status-critical)" stroke-width="2"/>
           <line x1="${cx - 4}" y1="${cy + 4}" x2="${cx + 4}" y2="${cy - 4}" stroke="var(--status-critical)" stroke-width="2"/>
           <circle cx="${cx}" cy="${cy}" r="9" fill="transparent"/></g>`
        : `<g class="pt" data-i="${i}"><circle cx="${cx}" cy="${cy}" r="3" fill="var(--series-1)"/>
           <circle cx="${cx}" cy="${cy}" r="9" fill="transparent"/></g>`;
    })
    .join("");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" id="spark">
    <line x1="${PAD}" y1="${H - PAD}" x2="${W - PAD}" y2="${H - PAD}" stroke="var(--baseline)"/>
    <text x="${PAD}" y="${y(max) - 6}">${fmtSecs(max)}</text>
    <path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2"/>
    ${marks}</svg>`;
}

async function renderModel(uid) {
  const d = await api("/models/" + encodeURIComponent(uid));
  const h = d.history;
  const rows = h
    .slice()
    .reverse()
    .map(
      (r) => `<tr class="clickable" onclick="location.hash='#/run/${esc(r.invocation_id)}'">
      <td>${fmtTime(r.generated_at)}</td><td>${statusHtml(r.status)}</td>
      <td class="num">${fmtSecs(r.execution_time)}</td><td>${esc(r.message ?? "")}</td></tr>`
    )
    .join("");
  view.innerHTML = `
    <div class="card"><h2>${esc(uid)}</h2>
      <p>${esc(d.node?.description || "")}</p>
      <h2>Duration across ${h.length} run${h.length === 1 ? "" : "s"}</h2>
      ${h.length ? sparkline(h) : '<div class="empty">No recorded runs for this node.</div>'}
      <div class="legend"><span><span class="swatch" style="background:var(--series-1)"></span>duration</span>
      <span style="color:var(--status-critical)">✗ failed run</span></div></div>
    <div class="card"><h2>Run log</h2>
      <table><thead><tr><th>When (UTC)</th><th>Status</th>
      <th style="text-align:right">Time</th><th>Message</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="card"><h2>SQL</h2><div id="sqlbox" class="empty">Loading…</div></div>`;

  document.querySelectorAll("#spark .pt").forEach((g) => {
    g.addEventListener("mousemove", (ev) => {
      const r = h[+g.dataset.i];
      showTip(ev, `${fmtTime(r.generated_at)}<br>${statusHtml(r.status)} · ${fmtSecs(r.execution_time)}`);
    });
    g.addEventListener("mouseleave", hideTip);
  });
  try {
    const sql = await api("/models/" + encodeURIComponent(uid) + "/sql");
    document.getElementById("sqlbox").outerHTML = `<pre class="sql">${esc(sql.raw_code || "(not available)")}</pre>`;
  } catch {
    document.getElementById("sqlbox").textContent = "Not available in the latest manifest.";
  }
}

/* ---------- diff ---------- */

async function renderDiff() {
  const data = await api("/runs?limit=100");
  if (data.runs.length < 2) {
    view.innerHTML = `<div class="card"><div class="empty">Need at least two recorded runs to diff.</div></div>`;
    return;
  }
  const opts = data.runs
    .map((r) => `<option value="${esc(r.invocation_id)}">${fmtTime(r.generated_at)} (${esc(r.status)})</option>`)
    .join("");
  view.innerHTML = `
    <div class="card"><h2>What changed between two runs</h2>
      <p>From <select id="a">${opts}</select> to <select id="b">${opts}</select>
      <button id="go">Diff</button></p><div id="diffout"></div></div>`;
  document.getElementById("a").selectedIndex = 1;
  const go = async () => {
    const a = document.getElementById("a").value, b = document.getElementById("b").value;
    const d = await api(`/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    const col = (title, items) => `<div class="diff-col"><h3>${title} (${items.length})</h3>
      <ul>${items.map((i) => `<li><a href="#/model/${esc(i)}">${esc(i.split(".").pop())}</a></li>`).join("") || "<li>none</li>"}</ul></div>`;
    document.getElementById("diffout").innerHTML =
      (d.engine_changed
        ? `<div class="banner">dbt major version changed between these runs - checksums are
           not comparable across engines, so "modified" is overstated here.</div>`
        : "") +
      col("Added", d.added) + col("Removed", d.removed) + col("Modified", d.modified) +
      `<div class="diff-col"><h3>Unchanged</h3><ul><li>${d.unchanged} nodes</li></ul></div>`;
  };
  document.getElementById("go").onclick = go;
  go();
}

/* ---------- DAG ---------- */

const CAT = { model: "--cat-model", seed: "--cat-seed", snapshot: "--cat-snapshot", source: "--cat-source", test: "--cat-test" };
const SHAPE = { model: "round-rectangle", seed: "ellipse", snapshot: "diamond", source: "barrel", test: "triangle" };

async function renderDag() {
  view.innerHTML = `<div class="card"><h2>Lineage</h2>
    <p><input id="search" placeholder="focus a node (e.g. orders)" size="30">
       <label style="margin-left:10px"><input type="checkbox" id="showtests"> tests</label>
       <span id="dagmeta" style="color:var(--ink-muted)"></span></p>
    <div id="dag"></div>
    <div class="legend">${Object.entries(CAT)
      .map(([t, v]) => `<span><span class="swatch" style="background:var(${v})"></span>${t}</span>`)
      .join("")}</div></div>`;
  const css = getComputedStyle(document.documentElement);
  const draw = async (focus) => {
    const t = document.getElementById("showtests").checked ? "true" : "false";
    const d = await api(`/dag?tests=${t}` + (focus ? `&node=${encodeURIComponent(focus)}` : ""));
    document.getElementById("dagmeta").textContent = d.too_large
      ? `${d.count} nodes - too many to draw at once; search to focus a neighborhood.`
      : `${d.nodes.length} of ${d.count} nodes`;
    if (d.too_large) return;
    cytoscape({
      container: document.getElementById("dag"),
      elements: [
        ...d.nodes.map((n) => ({ data: n })),
        ...d.edges.map((e) => ({ data: e })),
      ],
      layout: { name: "breadthfirst", directed: true, spacingFactor: 1.1 },
      style: [
        {
          selector: "node",
          style: {
            label: "data(name)",
            "font-size": 10,
            color: css.getPropertyValue("--ink-1").trim(),
            "text-valign": "bottom",
            "text-margin-y": 4,
            width: 26, height: 26,
            shape: (el) => SHAPE[el.data("type")] || "ellipse",
            "background-color": (el) =>
              css.getPropertyValue(CAT[el.data("type")] || "--ink-muted").trim(),
          },
        },
        {
          selector: "edge",
          style: {
            width: 1.5,
            "line-color": css.getPropertyValue("--baseline").trim(),
            "target-arrow-shape": "triangle",
            "target-arrow-color": css.getPropertyValue("--baseline").trim(),
            "curve-style": "bezier",
          },
        },
      ],
    }).on("tap", "node", (ev) => (location.hash = "#/model/" + ev.target.id()));
  };
  let t;
  document.getElementById("search").addEventListener("input", (ev) => {
    clearTimeout(t);
    const q = ev.target.value.trim();
    t = setTimeout(async () => {
      if (!q) return draw();
      const d = await api("/dag");
      const hit = (d.nodes || []).find((n) => n.name?.includes(q) || n.id.includes(q));
      draw(hit ? hit.id : "model.__none__." + q);
    }, 300);
  });
  document.getElementById("showtests").addEventListener("change", () => draw());
  draw();
}

/* ---------- router ---------- */

async function route() {
  const hash = location.hash || "#/runs";
  document.querySelectorAll("[data-nav]").forEach((a) => {
    a.classList.toggle("active", hash.startsWith("#/" + a.dataset.nav) ||
      (a.dataset.nav === "runs" && (hash.startsWith("#/run/") || hash.startsWith("#/model/"))));
  });
  try {
    if (hash.startsWith("#/run/")) await renderRun(decodeURIComponent(hash.slice(6)));
    else if (hash.startsWith("#/model/")) await renderModel(decodeURIComponent(hash.slice(8)));
    else if (hash.startsWith("#/diff")) await renderDiff();
    else if (hash.startsWith("#/dag")) await renderDag();
    else await renderRuns();
  } catch (e) {
    view.innerHTML = `<div class="card"><div class="empty">Error: ${esc(e.message)}</div></div>`;
  }
  api("/summary").then((s) => {
    document.getElementById("summary").textContent =
      `${s.runs} runs · ${s.models} models` +
      (s.last_run ? ` · last: ${s.last_run.status} ${fmtTime(s.last_run.generated_at)}` : "");
  }).catch(() => {});
}

addEventListener("hashchange", route);
route();
