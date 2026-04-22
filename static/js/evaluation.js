/* ═══════════════════════════════════════════════════════════════════════
   PowerCast — evaluation.js
   ─────────────────────────────────────────────────────────────────────
   Renders three Plotly charts and a sortable/filterable/paginated table
   using data injected from Flask via <script type="application/json"> tags.

   Charts:
     1. avpChart      — Actual vs Predicted line chart (dual traces)
     2. errHistChart  — Error distribution histogram
     3. residChart    — Residuals over time scatter

   All charts share the same dark trading-platform theme as forecast.js.
═══════════════════════════════════════════════════════════════════════ */

'use strict';

/* ──────────────────────────────────────────────────────────────────────
   READ DATA FROM JSON SCRIPT TAGS
   (Flask injects server-side data; no extra fetch needed)
────────────────────────────────────────────────────────────────────── */
function readJSON(id) {
  const el = document.getElementById(id);
  return el ? JSON.parse(el.textContent) : null;
}

const chartData = readJSON('chartDataJSON');  // { times, actual, predicted }
const residData = readJSON('residDataJSON');  // { times, values }
const histData  = readJSON('histDataJSON');   // { x, y }
const metrics   = readJSON('metricsJSON');    // { mae, rmse, mape, ... }

// Table data — kept in module state for sort/filter
let ALL_ROWS  = readJSON('tableDataJSON') || [];
let filtered  = [...ALL_ROWS];
let sortCol   = 0;
let sortAsc   = false;   // newest first by default
let page      = 1;
const PER_PAGE = 20;

/* ──────────────────────────────────────────────────────────────────────
   THEME / SHARED PLOTLY SETTINGS  (mirrors forecast.js)
────────────────────────────────────────────────────────────────────── */
const C = {
  bg:      '#0f172a',
  grid:    'rgba(99,102,241,0.08)',
  tick:    '#64748b',
  label:   '#94a3b8',
  accent:  '#6366f1',   // indigo  — predicted
  actual:  '#22c55e',   // green   — actual
  ma:      '#f59e0b',   // amber
  red:     '#ef4444',
  green:   '#10b981',
  font:    'Poppins, sans-serif',
};

const PLOTLY_CFG = {
  responsive:  true,
  displaylogo: false,
  modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
  toImageButtonOptions: { format: 'png', scale: 2 },
};

function baseLayout(extra) {
  return Object.assign({
    paper_bgcolor: C.bg,
    plot_bgcolor:  C.bg,
    font:     { family: C.font, color: C.label, size: 11 },
    hovermode:'x unified',
    dragmode: 'zoom',
    legend: {
      orientation: 'h', yanchor: 'bottom', y: 1.01,
      xanchor: 'right', x: 1,
      bgcolor: 'rgba(0,0,0,0)',
      font: { size: 11, color: C.label },
    },
    margin: { l: 65, r: 20, t: 30, b: 60 },
  }, extra);
}

function xAxisCfg(extra) {
  return Object.assign({
    gridcolor:      C.grid,
    linecolor:      'rgba(255,255,255,0.06)',
    tickfont:       { color: C.tick, family: C.font, size: 10 },
    showspikes:     true,
    spikecolor:     C.accent,
    spikethickness: 1,
    spikedash:      'dot',
    spikemode:      'across',
    spikesnap:      'cursor',
  }, extra);
}

function yAxisCfg(extra) {
  return Object.assign({
    gridcolor:      C.grid,
    linecolor:      'rgba(255,255,255,0.06)',
    tickfont:       { color: C.tick, family: C.font, size: 10 },
    zeroline:       false,
    showspikes:     true,
    spikecolor:     C.accent,
    spikethickness: 1,
  }, extra);
}

/* ══════════════════════════════════════════════════════════════════════
   CHART 1 — ACTUAL vs PREDICTED LINE CHART
   • Dual spline traces (green = actual, indigo = predicted)
   • Gradient fill under each line
   • Range slider + 6H/12H/1D/All buttons
   • Unified hover shows both values + error at that point
══════════════════════════════════════════════════════════════════════ */
function renderAvpChart() {
  if (!chartData) return;

  const times = chartData.times;
  const act   = chartData.actual;
  const pred  = chartData.predicted;

  // Error at each sampled point (for hover customdata)
  const err = act.map((a, i) => (a - pred[i]).toFixed(1));

  const traces = [
    // ── Actual fill ────────────────────────────────────────────────────
    {
      type: 'scatter', mode: 'none',
      x: times, y: act,
      fill: 'tozeroy',
      fillcolor: 'rgba(34,197,94,0.04)',
      hoverinfo: 'skip', showlegend: false, name: '_act_fill',
    },
    // ── Predicted fill ─────────────────────────────────────────────────
    {
      type: 'scatter', mode: 'none',
      x: times, y: pred,
      fill: 'tozeroy',
      fillcolor: 'rgba(99,102,241,0.05)',
      hoverinfo: 'skip', showlegend: false, name: '_pred_fill',
    },
    // ── Actual MCP line ────────────────────────────────────────────────
    {
      type: 'scatter', mode: 'lines',
      name: 'Actual MCP',
      x: times, y: act,
      customdata: err,
      line: { color: C.actual, width: 1.8, shape: 'spline', smoothing: 0.6 },
      hovertemplate:
        '<b>%{x}</b><br>' +
        'Actual:    ₹%{y:,.0f}/MWh<br>' +
        'Error:     ₹%{customdata}' +
        '<extra>Actual</extra>',
    },
    // ── Predicted MCP line ─────────────────────────────────────────────
    {
      type: 'scatter', mode: 'lines',
      name: 'Predicted MCP',
      x: times, y: pred,
      line: { color: C.accent, width: 1.8, shape: 'spline', smoothing: 0.6, dash: 'solid' },
      hovertemplate:
        '<b>%{x}</b><br>' +
        'Predicted: ₹%{y:,.0f}/MWh' +
        '<extra>Predicted</extra>',
    },
  ];

  const layout = baseLayout({
    xaxis: xAxisCfg({
      rangeslider: {
        visible: true, thickness: 0.05,
        bgcolor: 'rgba(255,255,255,0.02)',
        bordercolor: 'rgba(255,255,255,0.04)',
      },
      rangeselector: {
        buttons: [
          { count: 6,  label: '6H',  step: 'hour', stepmode: 'backward' },
          { count: 12, label: '12H', step: 'hour', stepmode: 'backward' },
          { count: 1,  label: '1D',  step: 'day',  stepmode: 'backward' },
          { count: 7,  label: '1W',  step: 'day',  stepmode: 'backward' },
          { step: 'all', label: 'All' },
        ],
        bgcolor: 'rgba(255,255,255,0.04)', activecolor: 'rgba(99,102,241,0.35)',
        bordercolor: 'rgba(255,255,255,0.07)', font: { color: C.label, size: 11 },
        x: 0, y: 1.08,
      },
    }),
    yaxis: yAxisCfg({
      tickprefix: '₹',
      title: { text: 'MCP (₹/MWh)', font: { size: 12, color: C.label } },
    }),
    // Mean error annotation
    annotations: [{
      x: 1, xref: 'paper', y: metrics ? metrics.mae : 0, yref: 'y',
      text: `MAE ₹${metrics ? metrics.mae.toLocaleString('en-IN') : '?'}`,
      showarrow: false, xanchor: 'right', yanchor: 'bottom',
      font: { color: 'rgba(255,255,255,0.25)', size: 9 },
    }],
  });

  Plotly.newPlot('avpChart', traces, layout, PLOTLY_CFG);
}

/* ══════════════════════════════════════════════════════════════════════
   CHART 2 — ERROR DISTRIBUTION HISTOGRAM
   • Shows how prediction errors are spread
   • Colour-coded: bars near zero = green, outliers = red
   • Vertical reference line at zero
══════════════════════════════════════════════════════════════════════ */
function renderErrHistChart() {
  if (!histData) return;

  // Colour each bar by distance from zero
  const maxAbs = Math.max(...histData.x.map(Math.abs));
  const barColors = histData.x.map(x => {
    const t = Math.abs(x) / (maxAbs || 1);
    const r = Math.round(16  + t * (239 - 16));
    const g = Math.round(185 + t * (68  - 185));
    const b = Math.round(129 + t * (68  - 129));
    return `rgba(${r},${g},${b},0.8)`;
  });

  const trace = {
    type: 'bar',
    name: 'Error Count',
    x: histData.x, y: histData.y,
    marker: { color: barColors, cornerradius: 3 },
    hovertemplate: 'Error ₹%{x:,.0f}<br>Count: %{y}<extra></extra>',
  };

  const layout = baseLayout({
    hovermode: 'closest',
    showlegend: false,
    margin: { l: 55, r: 20, t: 10, b: 50 },
    xaxis: xAxisCfg({
      title: { text: 'Prediction Error (₹/MWh)', font: { size: 11 } },
      showspikes: false,
    }),
    yaxis: yAxisCfg({
      title: { text: 'Frequency', font: { size: 11 } },
      tickprefix: '',
      showspikes: false,
    }),
    // Zero line
    shapes: [{
      type: 'line', x0: 0, x1: 0, xref: 'x', y0: 0, y1: 1, yref: 'paper',
      line: { color: 'rgba(255,255,255,0.2)', width: 1.5, dash: 'dash' },
    }],
    annotations: [{
      x: 0, xref: 'x', y: 1, yref: 'paper',
      text: 'Zero Error', showarrow: false,
      xanchor: 'left', yanchor: 'top',
      font: { color: 'rgba(255,255,255,0.2)', size: 9 },
    }],
  });

  Plotly.newPlot('errHistChart', [trace], layout, PLOTLY_CFG);
}

/* ══════════════════════════════════════════════════════════════════════
   CHART 3 — RESIDUALS OVER TIME
   • Scatter of (actual − predicted) vs time
   • Horizontal zero reference line
   • Colour: positive residuals green, negative red
══════════════════════════════════════════════════════════════════════ */
function renderResidChart() {
  if (!residData) return;

  const times  = residData.times;
  const values = residData.values;

  // Split into positive (over-prediction) and negative (under-prediction)
  const posX = [], posY = [], negX = [], negY = [];
  values.forEach((v, i) => {
    if (v >= 0) { posX.push(times[i]); posY.push(v); }
    else        { negX.push(times[i]); negY.push(v); }
  });

  const traces = [
    {
      type: 'scatter', mode: 'markers',
      name: 'Under-predicted (actual > pred)',
      x: posX, y: posY,
      marker: { color: 'rgba(34,197,94,0.6)', size: 3, symbol: 'circle' },
      hovertemplate: '%{x}<br>Residual: ₹%{y:,.0f}<extra>+</extra>',
    },
    {
      type: 'scatter', mode: 'markers',
      name: 'Over-predicted (actual < pred)',
      x: negX, y: negY,
      marker: { color: 'rgba(239,68,68,0.6)', size: 3, symbol: 'circle' },
      hovertemplate: '%{x}<br>Residual: ₹%{y:,.0f}<extra>−</extra>',
    },
  ];

  const layout = baseLayout({
    hovermode: 'closest',
    margin: { l: 65, r: 20, t: 10, b: 50 },
    xaxis: xAxisCfg({
      title: { text: 'Time', font: { size: 11 } },
      showspikes: false,
    }),
    yaxis: yAxisCfg({
      tickprefix: '₹',
      title: { text: 'Residual (₹/MWh)', font: { size: 11 } },
    }),
    shapes: [{
      type: 'line', x0: 0, x1: 1, xref: 'paper',
      y0: 0, y1: 0, yref: 'y',
      line: { color: 'rgba(255,255,255,0.15)', width: 1.5, dash: 'dash' },
    }],
  });

  Plotly.newPlot('residChart', traces, layout, PLOTLY_CFG);
}

/* ──────────────────────────────────────────────────────────────────────
   TABLE — sort / filter / paginate
────────────────────────────────────────────────────────────────────── */
function renderEvalTable() {
  const total = filtered.length;
  const pages = Math.ceil(total / PER_PAGE);
  const start = (page - 1) * PER_PAGE;
  const slice = filtered.slice(start, start + PER_PAGE);

  document.getElementById('evalBody').innerHTML = slice.map(r => {
    const errClass = r.error > 0 ? 'eval-pos' : r.error < 0 ? 'eval-neg' : '';
    return `
      <tr>
        <td class="td-ts">${r.datetime}</td>
        <td class="td-price">₹${Number(r.actual).toLocaleString('en-IN', {minimumFractionDigits:2})}</td>
        <td class="td-price" style="color:var(--acc)">₹${Number(r.predicted).toLocaleString('en-IN', {minimumFractionDigits:2})}</td>
        <td class="td-dim ${errClass}">${r.error >= 0 ? '+' : ''}${r.error.toFixed(2)}</td>
        <td class="td-dim">${r.pct_error.toFixed(2)}%</td>
      </tr>`;
  }).join('');

  document.getElementById('evalRowInfo').textContent =
    `Showing ${start + 1}–${Math.min(start + PER_PAGE, total)} of ${total}`;

  buildPaginator(pages);
}

function filterEvalTable() {
  const q = (document.getElementById('evalSearch').value || '').toLowerCase();
  filtered = ALL_ROWS.filter(r => r.datetime.toLowerCase().includes(q));
  page = 1;
  renderEvalTable();
}

function sortEval(col) {
  sortAsc = sortCol === col ? !sortAsc : true;
  sortCol = col;
  const keys = ['datetime', 'actual', 'predicted', 'error', 'pct_error'];
  const k = keys[col];
  filtered.sort((a, b) => {
    const av = a[k] ?? '', bv = b[k] ?? '';
    return sortAsc ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1);
  });
  renderEvalTable();
}

function buildPaginator(pages) {
  if (pages <= 1) { document.getElementById('evalPaginator').innerHTML = ''; return; }
  const cur = page;
  let h = '';
  h += pgBtn('‹', cur > 1 ? cur - 1 : null);
  if (cur > 3) h += pgBtn('1', 1) + (cur > 4 ? '<span style="color:var(--txt3);padding:0 4px">…</span>' : '');
  for (let p = Math.max(1, cur - 2); p <= Math.min(pages, cur + 2); p++) h += pgBtn(p, p, p === cur);
  if (cur < pages - 2) h += (cur < pages - 3 ? '<span style="color:var(--txt3);padding:0 4px">…</span>' : '') + pgBtn(pages, pages);
  h += pgBtn('›', cur < pages ? cur + 1 : null);
  document.getElementById('evalPaginator').innerHTML = h;
}

function pgBtn(label, p, active = false) {
  if (p == null) return `<button class="pg-btn" disabled>${label}</button>`;
  return `<button class="pg-btn ${active ? 'active' : ''}" onclick="gotoEvalPage(${p})">${label}</button>`;
}
function gotoEvalPage(p) { page = p; renderEvalTable(); }

/* ──────────────────────────────────────────────────────────────────────
   INIT — render all on DOMContentLoaded
────────────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  renderAvpChart();
  renderErrHistChart();
  renderResidChart();
  renderEvalTable();
});
