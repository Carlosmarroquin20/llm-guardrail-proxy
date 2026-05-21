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
      --border: #2a313c;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #58a6ff;
      --ok: #3fb950;
      --warn: #d29922;
      --danger: #f85149;
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
    header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-bottom: 1.5rem;
      border-bottom: 1px solid var(--border);
      padding-bottom: 1rem;
    }
    h1 { font-size: 1.25rem; margin: 0; font-weight: 600; }
    h2 { font-size: 1rem; margin: 1.5rem 0 0.75rem; color: var(--muted); }
    .meta { font-family: var(--mono); font-size: 0.8125rem; color: var(--muted); }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 0.75rem;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.875rem 1rem;
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
    }
    .card .value.ok { color: var(--ok); }
    .card .value.warn { color: var(--warn); }
    .card .value.danger { color: var(--danger); }
    table {
      width: 100%;
      border-collapse: collapse;
      font-family: var(--mono);
      font-size: 0.8125rem;
    }
    th, td {
      padding: 0.5rem 0.75rem;
      text-align: left;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    th { color: var(--muted); font-weight: 500; }
    .verdict-allowed { color: var(--ok); }
    .verdict-rejected { color: var(--danger); }
    .finding { color: var(--warn); }
    .breakdown {
      display: flex;
      gap: 1rem;
      flex-wrap: wrap;
      margin-top: 0.5rem;
    }
    .breakdown div {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 0.375rem 0.625rem;
      font-family: var(--mono);
      font-size: 0.8125rem;
    }
    .empty { color: var(--muted); font-style: italic; }
  </style>
</head>
<body>
  <header>
    <h1>llm-guardrail-proxy &mdash; live stats</h1>
    <div class="meta">
      <span id="last-updated">never</span> &middot; refresh 5s
    </div>
  </header>

  <section>
    <div class="grid" id="summary-cards"></div>
    <h2>Verdict breakdown</h2>
    <div class="breakdown" id="breakdown"></div>
  </section>

  <section>
    <h2>Recent requests</h2>
    <table>
      <thead>
        <tr>
          <th>timestamp</th>
          <th>model</th>
          <th>verdict</th>
          <th>middleware</th>
          <th>tokens</th>
          <th>cost (usd)</th>
          <th>latency (ms)</th>
          <th>findings</th>
        </tr>
      </thead>
      <tbody id="recent"></tbody>
    </table>
  </section>

  <script>
    const fmt = {
      cost(v) { return v == null ? '—' : v; },
      tokens(v) { return v == null ? '—' : v; },
      latency(v) { return v == null ? '—' : Number(v).toFixed(2); },
      timestamp(v) {
        if (!v) return '—';
        const d = new Date(v);
        return d.toLocaleTimeString();
      },
    };

    function card(label, value, klass) {
      return `<div class="card">
        <div class="label">${label}</div>
        <div class="value ${klass || ''}">${value}</div>
      </div>`;
    }

    function renderSummary(s) {
      const cards = [
        card('total requests', s.total_requests),
        card('allowed', s.allowed, 'ok'),
        card('rejected', s.rejected, s.rejected > 0 ? 'danger' : ''),
        card(
          'rejection rate',
          (s.rejection_rate * 100).toFixed(1) + '%',
          s.rejection_rate > 0 ? 'warn' : 'ok',
        ),
        card('estimated cost (usd)', s.total_estimated_cost_usd),
        card('avg latency (ms)', s.avg_latency_ms.toFixed(2)),
      ];
      document.getElementById('summary-cards').innerHTML = cards.join('');

      const breakdown = document.getElementById('breakdown');
      const parts = [];
      const sections = [
        ['rejections by middleware', s.rejections_by_middleware],
        ['requests by model', s.requests_by_model],
        ['findings by scanner', s.findings_by_scanner],
      ];
      for (const [title, mapping] of sections) {
        const entries = Object.entries(mapping || {});
        if (!entries.length) continue;
        const items = entries
          .map(([k, v]) => `${k}=${v}`)
          .join('  ');
        parts.push(`<div><strong>${title}:</strong> ${items}</div>`);
      }
      breakdown.innerHTML = parts.length
        ? parts.join('')
        : '<div class="empty">no breakdown data yet</div>';
    }

    function renderRecent(rows) {
      const tbody = document.getElementById('recent');
      if (!rows.length) {
        tbody.innerHTML =
          '<tr><td colspan="8" class="empty">no requests recorded yet</td></tr>';
        return;
      }
      tbody.innerHTML = rows
        .map((r) => {
          const verdictClass =
            r.verdict === 'allowed' ? 'verdict-allowed' : 'verdict-rejected';
          const findings = (r.findings || [])
            .map(
              (f) =>
                `<div class="finding">${f.scanner}/${f.kind} (${f.preview})</div>`,
            )
            .join('');
          return `<tr>
            <td>${fmt.timestamp(r.timestamp)}</td>
            <td>${r.model || ''}</td>
            <td class="${verdictClass}">${r.verdict}</td>
            <td>${r.rejecting_middleware || ''}</td>
            <td>${fmt.tokens(r.token_count)}</td>
            <td>${fmt.cost(r.estimated_cost_usd)}</td>
            <td>${fmt.latency(r.latency_ms)}</td>
            <td>${findings || ''}</td>
          </tr>`;
        })
        .join('');
    }

    async function refresh() {
      try {
        const [summary, recent] = await Promise.all([
          fetch('/stats/summary', { cache: 'no-store' }).then((r) => r.json()),
          fetch('/stats/recent?limit=25', { cache: 'no-store' }).then((r) => r.json()),
        ]);
        renderSummary(summary);
        renderRecent(recent);
        document.getElementById('last-updated').textContent =
          'updated ' + new Date().toLocaleTimeString();
      } catch (err) {
        // Failure to refresh must never crash the page; the timestamp
        // stays at its last successful value so an operator can spot
        // staleness at a glance.
        console.error(err);
      }
    }

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""
