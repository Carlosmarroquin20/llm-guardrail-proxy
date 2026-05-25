"""Embedded HTML dashboard for the stats endpoint.

The dashboard is intentionally a single static document served from
``GET /stats/dashboard``. It polls ``/stats/summary`` and
``/stats/recent`` every five seconds via ``fetch`` — no JavaScript
framework, no build step, no extra runtime dependency.

Embedding the markup as a Python string (rather than loading it from a
sibling ``.html`` file) sidesteps the package-data distribution
question entirely: a wheel produced from this source tree will always
include the dashboard, regardless of how setuptools is configured.

The markup follows two stylistic constraints:

* No external assets (CDN fonts, Google Analytics, etc.). The proxy
  binds to localhost by default; pulling third-party resources into a
  page that visualises sensitive audit data would defeat the
  zero-egress promise.
* Render with default browser styles if the inline CSS fails to load
  for any reason. The structure must remain legible even unstyled.
"""

from __future__ import annotations

DASHBOARD_HTML: str = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>llm-guardrail-proxy</title>
  <style>
    :root {
      --bg: #0f1419;
      --panel: #161b22;
      --panel-hover: #1c2330;
      --border: #2a313c;
      --border-strong: #3a4250;
      --text: #e6edf3;
      --muted: #8b949e;
      --muted-strong: #b0bac6;
      --accent: #58a6ff;
      --ok: #3fb950;
      --warn: #d29922;
      --danger: #f85149;
      --info: #6c8eff;
      --mono: ui-monospace, "Cascadia Code", "JetBrains Mono", Menlo, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 2rem;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
            Roboto, "Helvetica Neue", Arial, sans-serif;
    }

    /* ---------------------- header ----------------------------- */
    header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-bottom: 1.5rem;
      border-bottom: 1px solid var(--border);
      padding-bottom: 1rem;
      gap: 1rem;
      flex-wrap: wrap;
    }
    h1 { font-size: 1.25rem; margin: 0; font-weight: 600; }
    h2 {
      font-size: 0.8125rem;
      margin: 1.75rem 0 0.75rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 600;
    }
    .meta {
      font-family: var(--mono);
      font-size: 0.8125rem;
      color: var(--muted);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .status-dot {
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--ok);
      flex-shrink: 0;
    }
    .status-dot.stale { background: var(--warn); }
    .status-dot.error { background: var(--danger); }

    /* ---------------------- summary cards ---------------------- */
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 0.75rem;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.875rem 1rem;
      position: relative;
      overflow: hidden;
    }
    .card .label {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
    }
    .card .value {
      font-family: var(--mono);
      font-size: 1.5rem;
      font-weight: 600;
      margin-top: 0.25rem;
      line-height: 1.2;
    }
    .card .value.ok { color: var(--ok); }
    .card .value.warn { color: var(--warn); }
    .card .value.danger { color: var(--danger); }
    .card .sub {
      margin-top: 0.25rem;
      font-size: 0.75rem;
      color: var(--muted);
      font-family: var(--mono);
    }

    /* ---------------------- breakdown bars --------------------- */
    .breakdown {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1.5rem;
    }
    .breakdown-section {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.875rem 1rem 1rem;
    }
    .breakdown-section .title {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      margin-bottom: 0.625rem;
    }
    .bar-row {
      display: grid;
      grid-template-columns: minmax(0, 7rem) 1fr auto;
      gap: 0.625rem;
      align-items: center;
      margin: 0.375rem 0;
      font-family: var(--mono);
      font-size: 0.8125rem;
    }
    .bar-label {
      color: var(--muted-strong);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .bar-track {
      background: var(--border);
      border-radius: 2px;
      height: 8px;
      overflow: hidden;
      position: relative;
    }
    .bar-fill {
      height: 100%;
      background: var(--accent);
      transition: width 0.3s ease;
    }
    .bar-row.danger .bar-fill { background: var(--danger); }
    .bar-row.warn .bar-fill { background: var(--warn); }
    .bar-count {
      color: var(--text);
      font-variant-numeric: tabular-nums;
      min-width: 2.5rem;
      text-align: right;
    }

    /* ---------------------- recent table ----------------------- */
    .table-wrap {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-family: var(--mono);
      font-size: 0.8125rem;
    }
    thead th {
      background: var(--bg);
      color: var(--muted);
      font-weight: 500;
      text-align: left;
      padding: 0.5rem 0.75rem;
      border-bottom: 1px solid var(--border-strong);
      position: sticky;
      top: 0;
    }
    tbody td {
      padding: 0.5rem 0.75rem;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover { background: var(--panel-hover); }
    .table-wrap.paused tbody tr:hover { outline: 1px solid var(--accent); }

    /* ---------------------- chips ------------------------------ */
    .chip {
      display: inline-block;
      padding: 0.125rem 0.5rem;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 500;
      background: var(--border);
      color: var(--muted-strong);
      white-space: nowrap;
    }
    .chip.provider-openai { background: rgba(63, 185, 80, 0.15); color: var(--ok); }
    .chip.provider-anthropic { background: rgba(108, 142, 255, 0.15); color: var(--info); }
    .chip.verdict-allowed { background: rgba(63, 185, 80, 0.15); color: var(--ok); }
    .chip.verdict-rejected { background: rgba(248, 81, 73, 0.18); color: var(--danger); }
    .chip.severity-high { background: rgba(248, 81, 73, 0.18); color: var(--danger); }
    .chip.severity-medium { background: rgba(210, 153, 34, 0.18); color: var(--warn); }
    .chip.severity-low { background: rgba(88, 166, 255, 0.15); color: var(--accent); }

    .finding-line {
      display: flex;
      gap: 0.375rem;
      align-items: center;
      margin: 0.125rem 0;
    }
    .finding-text { color: var(--muted-strong); }

    /* ---------------------- helpers ---------------------------- */
    .empty {
      color: var(--muted);
      font-style: italic;
      text-align: center;
      padding: 1.5rem;
    }
    .skeleton {
      background: linear-gradient(
        90deg,
        var(--border) 0%,
        var(--border-strong) 50%,
        var(--border) 100%
      );
      background-size: 200% 100%;
      animation: shimmer 1.5s ease-in-out infinite;
      border-radius: 4px;
      color: transparent;
    }
    @keyframes shimmer {
      0% { background-position: 200% 0; }
      100% { background-position: -200% 0; }
    }
    .num { font-variant-numeric: tabular-nums; }

    @media (max-width: 600px) {
      body { padding: 1rem; }
      header { flex-direction: column; align-items: flex-start; gap: 0.25rem; }
    }
  </style>
</head>
<body>
  <header>
    <h1>llm-guardrail-proxy &mdash; live stats</h1>
    <div class="meta">
      <span class="status-dot" id="status-dot" title="connection status"></span>
      <span id="last-updated">connecting&hellip;</span>
      <span>&middot;</span>
      <span>refresh 5s (pauses on hover)</span>
    </div>
  </header>

  <section>
    <div class="grid" id="summary-cards">
      <!-- Skeleton initial state — replaced on first successful refresh. -->
      <div class="card"><div class="label">total requests</div><div class="value skeleton">000</div></div>
      <div class="card"><div class="label">allowed</div><div class="value skeleton">000</div></div>
      <div class="card"><div class="label">rejected</div><div class="value skeleton">000</div></div>
      <div class="card"><div class="label">rejection rate</div><div class="value skeleton">00.0%</div></div>
      <div class="card"><div class="label">estimated cost</div><div class="value skeleton">$0.00</div></div>
      <div class="card"><div class="label">avg latency</div><div class="value skeleton">000 ms</div></div>
    </div>
  </section>

  <section>
    <h2>Breakdown</h2>
    <div class="breakdown" id="breakdown"></div>
  </section>

  <section>
    <h2>Recent requests</h2>
    <div class="table-wrap" id="table-wrap">
      <table>
        <thead>
          <tr>
            <th>time</th>
            <th>provider</th>
            <th>model</th>
            <th>verdict</th>
            <th>middleware</th>
            <th class="num">tokens</th>
            <th class="num">cost</th>
            <th class="num">latency</th>
            <th>findings</th>
          </tr>
        </thead>
        <tbody id="recent">
          <tr><td colspan="9" class="empty">waiting for first refresh&hellip;</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <script>
    // -------- formatting helpers ------------------------------------
    const STALE_AFTER_MS = 15_000;

    const fmt = {
      cost(v) {
        if (v == null) return '—';
        // Server emits Decimal as a string; render verbatim with a
        // currency prefix so the no-float-drift contract holds.
        return '$' + v;
      },
      tokens(v) {
        return v == null ? '—' : Number(v).toLocaleString();
      },
      latency(v) {
        if (v == null) return '—';
        return Number(v).toFixed(2) + ' ms';
      },
      relative(iso) {
        if (!iso) return '—';
        const then = new Date(iso).getTime();
        const delta = Math.max(0, Date.now() - then);
        if (delta < 1000) return 'just now';
        if (delta < 60_000) return Math.floor(delta / 1000) + 's ago';
        if (delta < 3_600_000) return Math.floor(delta / 60_000) + 'm ago';
        return Math.floor(delta / 3_600_000) + 'h ago';
      },
      absolute(iso) {
        if (!iso) return '';
        return new Date(iso).toLocaleString();
      },
      percent(v) {
        return (v * 100).toFixed(1) + '%';
      },
    };

    // -------- DOM builders -----------------------------------------
    function card(label, value, klass, sub) {
      return `<div class="card">
        <div class="label">${label}</div>
        <div class="value ${klass || ''}">${value}</div>
        ${sub ? `<div class="sub">${sub}</div>` : ''}
      </div>`;
    }

    function chip(text, klass) {
      return `<span class="chip ${klass || ''}">${text}</span>`;
    }

    function renderSummary(s) {
      const cards = [
        card('total requests', s.total_requests.toLocaleString()),
        card('allowed', s.allowed.toLocaleString(), 'ok'),
        card(
          'rejected',
          s.rejected.toLocaleString(),
          s.rejected > 0 ? 'danger' : '',
        ),
        card(
          'rejection rate',
          fmt.percent(s.rejection_rate),
          s.rejection_rate > 0.1
            ? 'danger'
            : s.rejection_rate > 0
            ? 'warn'
            : 'ok',
        ),
        card('estimated cost', fmt.cost(s.total_estimated_cost_usd), '', 'usd'),
        card('avg latency', fmt.latency(s.avg_latency_ms)),
      ];
      document.getElementById('summary-cards').innerHTML = cards.join('');
    }

    function renderBreakdown(s) {
      const sections = [
        ['rejections by middleware', s.rejections_by_middleware, 'danger'],
        ['requests by model', s.requests_by_model, ''],
        ['findings by scanner', s.findings_by_scanner, 'warn'],
      ];
      const parts = sections.map(([title, mapping, klass]) => {
        const entries = Object.entries(mapping || {});
        if (!entries.length) {
          return `<div class="breakdown-section">
            <div class="title">${title}</div>
            <div class="empty">no data</div>
          </div>`;
        }
        const max = Math.max(...entries.map(([_, v]) => v));
        const rows = entries
          .map(([k, v]) => {
            const width = max > 0 ? (v / max) * 100 : 0;
            return `<div class="bar-row ${klass}">
              <div class="bar-label" title="${k}">${k}</div>
              <div class="bar-track">
                <div class="bar-fill" style="width: ${width}%"></div>
              </div>
              <div class="bar-count">${v.toLocaleString()}</div>
            </div>`;
          })
          .join('');
        return `<div class="breakdown-section">
          <div class="title">${title}</div>
          ${rows}
        </div>`;
      });
      document.getElementById('breakdown').innerHTML = parts.join('');
    }

    function renderRecent(rows) {
      const tbody = document.getElementById('recent');
      if (!rows.length) {
        tbody.innerHTML =
          '<tr><td colspan="9" class="empty">no requests recorded yet — send one through the proxy</td></tr>';
        return;
      }
      tbody.innerHTML = rows
        .map((r) => {
          const providerClass = `provider-${(r.provider || '').toLowerCase()}`;
          const verdictClass = `verdict-${r.verdict}`;
          const findings = (r.findings || [])
            .map((f) => {
              const sev = (f.severity || 'low').toLowerCase();
              return `<div class="finding-line">
                ${chip(f.severity, `severity-${sev}`)}
                <span class="finding-text">${f.scanner}/${f.kind} &middot; ${f.preview}</span>
              </div>`;
            })
            .join('');
          return `<tr>
            <td title="${fmt.absolute(r.timestamp)}">${fmt.relative(r.timestamp)}</td>
            <td>${chip(r.provider, providerClass)}</td>
            <td>${r.model || ''}</td>
            <td>${chip(r.verdict, verdictClass)}</td>
            <td>${r.rejecting_middleware || ''}</td>
            <td class="num">${fmt.tokens(r.token_count)}</td>
            <td class="num">${fmt.cost(r.estimated_cost_usd)}</td>
            <td class="num">${fmt.latency(r.latency_ms)}</td>
            <td>${findings || ''}</td>
          </tr>`;
        })
        .join('');
    }

    // -------- refresh loop -----------------------------------------
    const tableWrap = document.getElementById('table-wrap');
    const statusDot = document.getElementById('status-dot');
    const lastUpdatedEl = document.getElementById('last-updated');

    // Pause-on-hover: while the user's mouse is over the recent table,
    // skip refreshes so a row does not vanish under the cursor.
    let paused = false;
    tableWrap.addEventListener('mouseenter', () => {
      paused = true;
      tableWrap.classList.add('paused');
    });
    tableWrap.addEventListener('mouseleave', () => {
      paused = false;
      tableWrap.classList.remove('paused');
    });

    let lastSuccessAt = 0;

    function updateStatus() {
      const now = Date.now();
      if (lastSuccessAt === 0) return;
      const age = now - lastSuccessAt;
      statusDot.classList.remove('stale', 'error');
      if (age > STALE_AFTER_MS) {
        statusDot.classList.add('stale');
        statusDot.title = `stale — last update ${Math.floor(age / 1000)}s ago`;
      } else {
        statusDot.title = 'live';
      }
      lastUpdatedEl.textContent = 'updated ' + fmt.relative(new Date(lastSuccessAt).toISOString());
    }

    async function refresh() {
      if (paused) return;
      try {
        const [summary, recent] = await Promise.all([
          fetch('/stats/summary', { cache: 'no-store' }).then((r) => {
            if (!r.ok) throw new Error('summary: ' + r.status);
            return r.json();
          }),
          fetch('/stats/recent?limit=25', { cache: 'no-store' }).then((r) => {
            if (!r.ok) throw new Error('recent: ' + r.status);
            return r.json();
          }),
        ]);
        renderSummary(summary);
        renderBreakdown(summary);
        renderRecent(recent);
        lastSuccessAt = Date.now();
        statusDot.classList.remove('error');
      } catch (err) {
        statusDot.classList.add('error');
        statusDot.title = 'refresh failed: ' + err.message;
        lastUpdatedEl.textContent = 'refresh failed';
        console.error(err);
      }
      updateStatus();
    }

    refresh();
    setInterval(refresh, 5000);
    // ``updateStatus`` runs on its own cadence so the "Xs ago" text
    // counts up even when the refresh loop is paused on hover.
    setInterval(updateStatus, 1000);
  </script>
</body>
</html>
"""
