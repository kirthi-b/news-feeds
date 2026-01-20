const GRADIENT_COLORS = [
  { bg: "rgba(167, 139, 250, 0.15)", fg: "#c4b5fd", border: "rgba(167, 139, 250, 0.4)" },
  { bg: "rgba(139, 92, 246, 0.15)", fg: "#a78bfa", border: "rgba(139, 92, 246, 0.4)" },
  { bg: "rgba(6, 182, 212, 0.15)", fg: "#67e8f9", border: "rgba(6, 182, 212, 0.4)" },
  { bg: "rgba(20, 184, 166, 0.15)", fg: "#5eead4", border: "rgba(20, 184, 166, 0.4)" },
  { bg: "rgba(56, 189, 248, 0.15)", fg: "#7dd3fc", border: "rgba(56, 189, 248, 0.4)" },
  { bg: "rgba(168, 85, 247, 0.15)", fg: "#c084fc", border: "rgba(168, 85, 247, 0.4)" },
  { bg: "rgba(99, 102, 241, 0.15)", fg: "#a5b4fc", border: "rgba(99, 102, 241, 0.4)" },
  { bg: "rgba(14, 165, 233, 0.15)", fg: "#7dd3fc", border: "rgba(14, 165, 233, 0.4)" },
];

function syncFixedLayoutVars() {
  const hw = document.querySelector('.header-wrapper');
  const panel = document.querySelector('.panel');
  if (hw) document.documentElement.style.setProperty('--header-h', hw.offsetHeight + 'px');
  if (panel) document.documentElement.style.setProperty('--panel-h', panel.offsetHeight + 'px');
}
window.addEventListener('resize', syncFixedLayoutVars);

function hashIndex(str, mod) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h) % mod;
}

function gradientPillStyle(label) {
  const c = GRADIENT_COLORS[hashIndex(label || "", GRADIENT_COLORS.length)];
  return `background:${c.bg}; color:${c.fg}; border-color:${c.border};`;
}

function esc(s){
  return (s||"")
    .replace(/&/g,"&amp;")
    .replace(/</g,"&lt;")
    .replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;");
}

function mkId(prefix, text) {
  return prefix + "_" + btoa(unescape(encodeURIComponent(text)))
    .replace(/=+$/,"")
    .replace(/[+/]/g,"_");
}

function closeAllQTooltips(exceptEl = null) {
  document.querySelectorAll('.qtooltip.open').forEach(tt => {
    if (exceptEl && tt === exceptEl) return;
    tt.classList.remove('open');
  });
}

function formatInlineBundleExclusions(exList) {
  if (!Array.isArray(exList) || exList.length === 0) return "";
  const uniq = [...new Set(exList.map(x => (x || "").trim()).filter(Boolean))];
  if (!uniq.length) return "";

  // Display compact; still full list via tooltip is not requested.
  const shown = uniq.slice(0, 4);
  const more = uniq.length > shown.length ? ` +${uniq.length - shown.length}` : "";

  const bits = shown.map(x => `<span class="ex-item">-${esc(x)}</span>`).join(" ");
  return `
    <span class="bundle-inline-exclusions" title="${esc(uniq.map(x => "-" + x).join(" "))}">
      <span class="ex-label">Exclusions:</span> ${bits}${more ? ` <span class="ex-item">${more}</span>` : ""}
    </span>
  `;
}

function queryTooltipHtml(exList) {
  const uniq = [...new Set((exList || []).map(x => (x || "").trim()).filter(Boolean))];
  if (!uniq.length) return "";
  const lines = uniq.map(x => `<div class="qt-line">-${esc(x)}</div>`).join("");
  return `
    <div class="qtooltip" role="dialog" aria-label="Query exclusions">
      <div class="qt-title">Exclusions</div>
      ${lines}
    </div>
  `;
}

async function load() {
  const res = await fetch("data.json", { cache: "no-store" });
  if (!res.ok) {
    document.getElementById("list").innerHTML = `<div class="empty">No data yet.</div>`;
    syncFixedLayoutVars();
    return;
  }

  const data = await res.json();
  const last = data.meta?.generated_at ? new Date(data.meta.generated_at) : null;
  document.getElementById("lastUpdated").textContent =
    "Last updated: " + (last ? last.toLocaleString() : "—");

  const items = Array.isArray(data.items) ? data.items : [];

  const bundleExclusions = data.meta?.bundle_exclusions || {};
  const queryExclusions = data.meta?.query_exclusions || {};

  // bundle -> set(queries) derived from items (for filtering list)
  const bundleMap = new Map();
  for (const it of items) {
    const b = it.bundle || "";
    const q = it.query || "";
    if (!b) continue;
    if (!bundleMap.has(b)) bundleMap.set(b, new Set());
    if (q) bundleMap.get(b).add(q);
  }

  const filters = document.getElementById("filters");
  const bundleState = new Map();
  const queryState = new Map();
  const expandState = new Map();

  const bundles = [...bundleMap.keys()].sort((a,b)=>a.localeCompare(b));
  for (const b of bundles) expandState.set(b, false);

  filters.innerHTML = bundles.map(b => {
    const bid = mkId("b", b);
    const qs = [...bundleMap.get(b)].sort((a,c)=>a.localeCompare(c));

    const bEx = Array.isArray(bundleExclusions[b]) ? bundleExclusions[b] : [];
    const inlineEx = formatInlineBundleExclusions(bEx);

    const children = qs.map(q => {
      const qid = mkId("q", b + "||" + q);

      const qEx = (queryExclusions[b] && Array.isArray(queryExclusions[b][q]))
        ? queryExclusions[b][q]
        : [];

      const hasQEx = Array.isArray(qEx) && qEx.length > 0;

      return `
        <label class="child">
          <input type="checkbox" id="${qid}" checked />
          <span class="qrow">
            <span>${esc(q)}</span>
            ${
              hasQEx
                ? `
                  <button type="button" class="qinfo-btn" data-qinfo="${esc(b)}||${esc(q)}" aria-label="Show exclusions">?</button>
                  ${queryTooltipHtml(qEx)}
                `
                : ""
            }
          </span>
        </label>
      `;
    }).join("");

    return `
      <div class="filter-group">
        <div class="filter-header" data-bundle="${esc(b)}">
          <span class="expand-icon">▶</span>

          <label onclick="event.stopPropagation()">
            <input type="checkbox" id="${bid}" checked />
            <span style="display:inline-flex; align-items:center; gap:8px; min-width:0;">
              <span class="pill" style="${gradientPillStyle(b)}">${esc(b)}</span>
              ${inlineEx}
            </span>
          </label>
        </div>

        <div class="filter-children" data-bundle-children="${esc(b)}">
          ${children}
        </div>
      </div>
    `;
  }).join("");

  for (const b of bundles) bundleState.set(b, true);
  for (const b of bundles) for (const q of bundleMap.get(b)) queryState.set(b + "||" + q, true);

  // Expand/collapse per bundle
  for (const b of bundles) {
    const header = document.querySelector(`.filter-header[data-bundle="${CSS.escape(b)}"]`);
    const children = document.querySelector(`.filter-children[data-bundle-children="${CSS.escape(b)}"]`);
    const icon = header.querySelector('.expand-icon');

    header.addEventListener('click', (e) => {
      if (e.target.tagName === 'INPUT') return;
      if (e.target.closest('.qinfo-btn') || e.target.closest('.qtooltip')) return;

      const isExpanded = expandState.get(b);
      expandState.set(b, !isExpanded);

      if (!isExpanded) {
        icon.classList.add('expanded');
        children.classList.add('expanded');
      } else {
        icon.classList.remove('expanded');
        children.classList.remove('expanded');
      }
      syncFixedLayoutVars();
    });
  }

  // Query tooltips: tap-to-toggle (mobile); hover handled by CSS on desktop
  document.querySelectorAll('.qinfo-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();

      const qrow = btn.closest('.qrow');
      const tt = qrow ? qrow.querySelector('.qtooltip') : null;
      if (!tt) return;

      const willOpen = !tt.classList.contains('open');
      closeAllQTooltips(tt);
      if (willOpen) tt.classList.add('open');
      else tt.classList.remove('open');

      syncFixedLayoutVars();
    });
  });

  // close on outside click (mobile)
  document.addEventListener('click', (e) => {
    if (e.target.closest('.qrow')) return;
    closeAllQTooltips();
  });

  // Bundle + query checkbox behavior
  let currentPage = 1;
  const itemsPerPage = 25;

  for (const b of bundles) {
    const bid = mkId("b", b);
    const bEl = document.getElementById(bid);

    bEl.addEventListener("change", () => {
      const checked = bEl.checked;
      bundleState.set(b, checked);
      for (const q of bundleMap.get(b)) {
        const qid = mkId("q", b + "||" + q);
        const qEl = document.getElementById(qid);
        qEl.checked = checked;
        queryState.set(b + "||" + q, checked);
      }
      currentPage = 1;
      render();
    });

    for (const q of bundleMap.get(b)) {
      const qid = mkId("q", b + "||" + q);
      const qEl = document.getElementById(qid);
      qEl.addEventListener("change", () => {
        queryState.set(b + "||" + q, qEl.checked);
        const any = [...bundleMap.get(b)].some(x => queryState.get(b + "||" + x));
        bEl.checked = any;
        bundleState.set(b, any);
        currentPage = 1;
        render();
      });
    }
  }

  const search = document.getElementById("search");
  const sort = document.getElementById("sort");
  const count = document.getElementById("count");

  function passesFilters(it) {
    const b = it.bundle || "";
    const q = it.query || "";
    if (!b) return false;
    if (!q) return !!bundleState.get(b);
    return !!queryState.get(b + "||" + q);
  }

  function updatePagination(totalPages) {
    const paginationEl = document.getElementById('pagination');
    if (totalPages <= 1) {
      paginationEl.innerHTML = '';
      return;
    }

    paginationEl.innerHTML = `
      <div class="page-info">Page ${currentPage} of ${totalPages}</div>
      <div class="pagination-buttons">
        <button id="prevBtn" ${currentPage === 1 ? 'disabled' : ''}>← Prev</button>
        <button id="nextBtn" ${currentPage === totalPages ? 'disabled' : ''}>Next →</button>
      </div>
    `;

    document.getElementById('prevBtn')?.addEventListener('click', () => {
      if (currentPage > 1) {
        currentPage--;
        render();
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }
    });

    document.getElementById('nextBtn')?.addEventListener('click', () => {
      if (currentPage < totalPages) {
        currentPage++;
        render();
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }
    });
  }

  function render() {
    const qtxt = (search.value || "").toLowerCase().trim();

    let filtered = items.filter(it => {
      if (!passesFilters(it)) return false;
      if (!qtxt) return true;
      const hay = `${it.title||""} ${it.source||""}`.toLowerCase();
      return hay.includes(qtxt);
    });

    const sortMode = sort.value;
    if (sortMode === "new") filtered.sort((a,b)=> (b.published_ts||0)-(a.published_ts||0));
    if (sortMode === "old") filtered.sort((a,b)=> (a.published_ts||0)-(b.published_ts||0));
    if (sortMode === "az") filtered.sort((a,b)=> (a.title||"").localeCompare(b.title||""));

    count.textContent = `${filtered.length} items`;

    const totalPages = Math.max(1, Math.ceil(filtered.length / itemsPerPage));
    if (currentPage > totalPages) currentPage = totalPages;

    const startIdx = (currentPage - 1) * itemsPerPage;
    const pageItems = filtered.slice(startIdx, startIdx + itemsPerPage);

    const list = document.getElementById("list");
    if (!filtered.length) {
      list.innerHTML = `<div class="empty">No matches.</div>`;
      updatePagination(0);
      syncFixedLayoutVars();
      return;
    }

    list.innerHTML = pageItems.map((it, idx) => {
      const d = it.published_ts ? new Date(it.published_ts*1000).toLocaleString() : "—";
      const bundle = esc(it.bundle || "");
      const src = esc(it.source || "");
      const title = esc(it.title || "Untitled");
      const url = it.canonical_url || it.url || "#";
      const qtag = it.query ? `<div class="qtag">${esc(it.query)}</div>` : "";

      return `
        <div class="card" style="animation-delay: ${Math.min(idx * 0.05, 0.3)}s">
          <div class="meta">
            <span class="pill" style="${gradientPillStyle(it.bundle || "")}">${bundle}</span>
            <span>${src}</span>
            <span class="date">${d}</span>
          </div>
          <div style="margin-top:4px;">
            <a href="${url}" target="_blank" rel="noopener noreferrer">
              <div class="title">${title}</div>
            </a>
          </div>
          ${qtag}
        </div>
      `;
    }).join("");

    updatePagination(totalPages);
    syncFixedLayoutVars();
  }

  search.addEventListener("input", () => { currentPage = 1; render(); });
  sort.addEventListener("change", () => { currentPage = 1; render(); });

  render();
  syncFixedLayoutVars();
}

function toggleMobileFilters() {
  const container = document.querySelector('.filters-container');
  const icon = document.querySelector('.mobile-toggle-icon');
  container.classList.toggle('expanded');
  icon.classList.toggle('expanded');

  syncFixedLayoutVars();
  setTimeout(syncFixedLayoutVars, 350);
}

document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("mobileFilterToggle");
  if (toggle) toggle.addEventListener("click", toggleMobileFilters);
  syncFixedLayoutVars();
  load();
});
