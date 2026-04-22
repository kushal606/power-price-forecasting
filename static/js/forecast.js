/* ═══════════════════════════════════════════════════════════════════════
   PowerCast — forecast.js
   ─────────────────────────────────────────────────────────────────────
   Charts (all Plotly, all interactive):
     1. renderMainChart()    — spline line + gradient fill + MA + crosshair
     2. renderCandleChart()  — daily OHLC candlestick (green/red)
     3. renderHeatmap()      — hour × day price heatmap (Plasma colorscale)
     4. renderDailyChart()   — gradient bar chart with ₹ hover

   API calls (unchanged from original):
     /api/forecast           — horizon-based POST
     /api/forecast_custom    — date-range POST
     /api/job/<id>           — background job poll
     /api/model_info         — date-picker init
     /api/download_forecast  — Excel download
═══════════════════════════════════════════════════════════════════════ */

'use strict';

/* ──────────────────────────────────────────────────────────────────────
   STATE
────────────────────────────────────────────────────────────────────── */
const S = {
  mode:         'horizon',
  horizon:      'weekly',
  forecastData: [],
  filtered:     [],
  page:         1,
  rowsPerPage:  25,
  sortCol:      0,
  sortAsc:      true,
  pollTimer:    null,
  jobId:        null,
  pollCount:    0,
  // ── NEW ──────────────────────────────────
  compareMode:  false,     // multi-day comparison toggle
  lastData:     null,      // cached forecast payload for re-renders
};

const HORIZON_META = {
  daily:    { label: '96 blocks · 24 hours' },
  weekly:   { label: '672 blocks · 7 days · background job' },
  monthly:  { label: '2,880 blocks · 30 days · background job' },
  seasonal: { label: '8,640 blocks · 90 days · background job' },
};

/* ──────────────────────────────────────────────────────────────────────
   PLOTLY GLOBAL CONFIG & THEME
────────────────────────────────────────────────────────────────────── */

// Shared Plotly config applied to every chart
const PLOTLY_CFG = {
  responsive:   true,
  displaylogo:  false,
  modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
  toImageButtonOptions: { format: 'png', scale: 2 },
};

// Trading-platform dark palette
const C = {
  bg:       '#0f172a',   // deep navy — chart paper
  surface:  '#111827',   // card surface
  grid:     'rgba(99,102,241,0.08)',
  tick:     '#64748b',
  label:    '#94a3b8',
  accent:   '#6366f1',   // indigo — main line
  accentDim:'rgba(99,102,241,0.15)',
  ma:       '#f59e0b',   // amber  — moving average
  green:    '#10b981',   // emerald — bullish candle
  red:      '#ef4444',   // rose   — bearish candle
  font:     'Poppins, sans-serif',
};

// Shared axis defaults
function xAxis(extra) {
  return Object.assign({
    gridcolor:      C.grid,
    linecolor:      'rgba(255,255,255,0.06)',
    tickfont:       { color: C.tick, family: C.font, size: 11 },
    showspikes:     true,
    spikecolor:     C.accent,
    spikethickness: 1,
    spikedash:      'dot',
    spikemode:      'across',
    spikesnap:      'cursor',   // spike follows mouse precisely
  }, extra);
}
function yAxis(extra) {
  return Object.assign({
    gridcolor:      C.grid,
    linecolor:      'rgba(255,255,255,0.06)',
    tickfont:       { color: C.tick, family: C.font, size: 11 },
    tickprefix:     '₹',
    zeroline:       false,
    showspikes:     true,
    spikecolor:     C.accent,
    spikethickness: 1,
    spikesnap:      'cursor',
  }, extra);
}
function baseLayout(extra) {
  return Object.assign({
    paper_bgcolor: C.bg,
    plot_bgcolor:  C.bg,
    font:          { family: C.font, color: C.label, size: 11 },
    hovermode:     'x unified',
    dragmode:      'zoom',
    legend: {
      orientation: 'h',
      yanchor: 'bottom', y: 1.01,
      xanchor: 'right',  x: 1,
      bgcolor: 'rgba(0,0,0,0)',
      font:    { size: 11, color: C.label },
    },
  }, extra);
}

/* ──────────────────────────────────────────────────────────────────────
   INIT
────────────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initEmptyChart();
  loadDatasetBounds();
  updateHorizonInfo('weekly');
});

/* ──────────────────────────────────────────────────────────────────────
   DATASET BOUNDS (date-picker init)
────────────────────────────────────────────────────────────────────── */
function loadDatasetBounds() {
  fetch('/api/model_info')
    .then(r => r.json())
    .then(d => {
      if (d.status !== 'success') return;
      const hint = document.getElementById('startHint');
      if (hint) {
        hint.textContent = `Dataset ends ${d.dataset_end_fmt}. Forecast from ${d.min_start_date} onwards.`;
      }
      const minDate = d.min_start_date;
      const startEl = document.getElementById('startDate');
      const endEl   = document.getElementById('endDate');
      if (startEl) startEl.min = minDate;
      if (endEl)   endEl.min   = minDate;
      const startDate = new Date(minDate + 'T00:00:00');
      const endDate   = new Date(startDate);
      endDate.setDate(startDate.getDate() + 7);
      if (startEl) startEl.value = fmtDate(startDate);
      if (endEl)   endEl.value   = fmtDate(endDate);
    })
    .catch(err => console.warn('[PowerCast] model_info failed:', err));
}

/* ──────────────────────────────────────────────────────────────────────
   MODE / HORIZON CONTROLS
────────────────────────────────────────────────────────────────────── */
function switchMode(mode) {
  S.mode = mode;
  document.getElementById('modeHorizon').classList.toggle('hidden', mode !== 'horizon');
  document.getElementById('modeCustom').classList.toggle('hidden',  mode !== 'custom');
  document.getElementById('tabHorizon').classList.toggle('active', mode === 'horizon');
  document.getElementById('tabCustom').classList.toggle('active',  mode === 'custom');
}
function selectChip(btn) {
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  S.horizon = btn.dataset.horizon;
  updateHorizonInfo(S.horizon);
}
function updateHorizonInfo(h) {
  const el = document.getElementById('horizonInfo');
  if (el) el.textContent = HORIZON_META[h]?.label ?? '';
}

/* ──────────────────────────────────────────────────────────────────────
   API — HORIZON FORECAST
────────────────────────────────────────────────────────────────────── */
function runHorizonForecast() {
  stopPolling();
  setLoading(true, 'Submitting forecast…');
  hideAlert();
  clearResults();

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/forecast', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.timeout = 30000;

  xhr.onload = function () {
    const raw = safeJSON(xhr.responseText);
    if (!raw) { handleError('Non-JSON response (HTTP ' + xhr.status + ')', xhr.responseText); return; }
    if (xhr.status === 202 && raw.status === 'accepted') {
      S.jobId     = raw.job_id;
      S.pollCount = 0;
      setLoading(true, `Job started [${S.jobId}] — polling…`);
      S.pollTimer = setInterval(pollJob, 3000);
      return;
    }
    if (xhr.status === 200 && raw.status === 'success') {
      setLoading(false);
      onSuccess(raw.data);
      return;
    }
    handleError(raw.message || 'HTTP ' + xhr.status, JSON.stringify(raw, null, 2));
  };
  xhr.onerror   = () => handleError('Network error — is Flask running on port 5000?');
  xhr.ontimeout = () => handleError('Timeout (30 s) — check Flask terminal');
  xhr.send(JSON.stringify({ horizon: S.horizon }));
}

/* ──────────────────────────────────────────────────────────────────────
   API — CUSTOM DATE FORECAST
────────────────────────────────────────────────────────────────────── */
function runCustomForecast() {
  stopPolling();
  const startDate = document.getElementById('startDate').value.trim();
  const endDate   = document.getElementById('endDate').value.trim();
  if (!startDate || !endDate) { showAlert('err', 'Select both Start and End dates.'); return; }
  if (new Date(endDate) <= new Date(startDate)) { showAlert('err', 'End must be after Start.'); return; }

  setLoading(true, 'Running custom forecast…');
  hideAlert();
  clearResults();

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/forecast_custom', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.timeout = 120000;

  xhr.onload = function () {
    setLoading(false);
    const raw = safeJSON(xhr.responseText);
    if (!raw) { handleError('Non-JSON response (HTTP ' + xhr.status + ')', xhr.responseText); return; }
    if (xhr.status === 200 && raw.status === 'success') { onSuccess(raw.data); return; }
    handleError(raw.message || 'HTTP ' + xhr.status, JSON.stringify(raw, null, 2));
  };
  xhr.onerror   = () => { setLoading(false); handleError('Network error'); };
  xhr.ontimeout = () => { setLoading(false); handleError('Timeout — try a smaller date range'); };
  xhr.send(JSON.stringify({ start_date: startDate, end_date: endDate }));
}

/* ──────────────────────────────────────────────────────────────────────
   BACKGROUND JOB POLLING
────────────────────────────────────────────────────────────────────── */
function pollJob() {
  if (!S.jobId) return;
  S.pollCount++;
  const msgs = ['Running forecast engine…','Computing lag features…','Iterating predictions…',
                'Building time series…','Almost done…'];
  setLoading(true, msgs[S.pollCount % msgs.length] + ` (${S.pollCount * 3}s)`);

  const xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/job/' + S.jobId, true);
  xhr.timeout = 10000;
  xhr.onload = function () {
    const raw = safeJSON(xhr.responseText);
    if (!raw) return;
    if (raw.status === 'success') { stopPolling(); setLoading(false); onSuccess(raw.data); }
    else if (raw.status === 'error') { stopPolling(); setLoading(false); handleError('Job failed: ' + (raw.message || '')); }
  };
  xhr.send();
}
function stopPolling() {
  clearInterval(S.pollTimer);
  S.pollTimer = null;
  S.jobId     = null;
}

/* ──────────────────────────────────────────────────────────────────────
   ON SUCCESS — orchestrate all renders
────────────────────────────────────────────────────────────────────── */
function onSuccess(d) {
  showAlert('ok',
    `✅ Forecast complete — ${d.n_blocks.toLocaleString()} blocks`
    + (d.n_days ? ` · ${d.n_days} days` : '')
    + ` · ${d.start_date} → ${d.end_date}`
  );

  // ── Cache for re-renders (compare mode, peak toggle) ──────────────────
  S.lastData = d;
  // Reset compare mode on new forecast
  S.compareMode = false;
  updateCompareBtn();

  renderKPIs(d);
  renderMainChart(d);   // includes peaks, hover panel wiring

  // Candlestick, heatmap and daily bar require daily-level data
  if (d.daily_avg && d.daily_avg.length >= 2) {
    renderCandleChart(d);
    renderHeatmap(d);
    renderDailyChart(d);
    show('candleCard');
    show('heatCard');
    show('dailyCard');
  } else {
    renderDailyChart(d);
    show('dailyCard');
  }

  document.getElementById('chartTitle').textContent =
    `MCP Forecast  |  ${d.start_date}  →  ${d.end_date}`;

  // Populate table
  S.forecastData = d.times.map((t, i) => ({
    ts:    t,
    price: d.prices[i],
    day:   d.daily_labels
             ? d.daily_labels[Math.floor(i * (d.daily_avg?.length || 1) / d.times.length)]
             : '—',
    hour:  t.split(' ')[1] || '—',
  }));
  S.filtered = [...S.forecastData];
  S.page     = 1;
  renderTable();
  show('tableCard');

  const dlBtn = document.getElementById('dlBtn');
  if (dlBtn) dlBtn.disabled = false;
}

/* ──────────────────────────────────────────────────────────────────────
   KPI CARDS
────────────────────────────────────────────────────────────────────── */
function renderKPIs(d) {
  const s   = d.summary || {};
  const set = (id, val) => {
    const el = document.querySelector(`#${id} .kpi-val`);
    if (el) el.textContent = val;
  };
  set('kMin',    s.min  != null ? '₹' + Number(s.min).toLocaleString('en-IN')  : '—');
  set('kMax',    s.max  != null ? '₹' + Number(s.max).toLocaleString('en-IN')  : '—');
  set('kMean',   s.mean != null ? '₹' + Number(s.mean).toLocaleString('en-IN') : '—');
  set('kStd',    s.std  != null ? '₹' + Number(s.std).toLocaleString('en-IN')  : '—');
  set('kBlocks', d.n_blocks != null ? d.n_blocks.toLocaleString() : '—');
  set('kDays',   d.n_days   != null ? d.n_days : Math.ceil((d.n_blocks || 0) / 96));
}

/* ══════════════════════════════════════════════════════════════════════
   CHART 1 — MAIN LINE CHART
   • Spline + gradient fill + moving average
   • Peak/Low markers with annotations (top-5 / bottom-5)
   • 6H / 12H / 1D / All range selector buttons
   • plotly_hover event → live hover panel
   • plotly_unhover → hide hover panel
   • Compare-days overlay (multi-line per day)
══════════════════════════════════════════════════════════════════════ */

function initEmptyChart() {
  document.getElementById('chartSpinner').classList.add('hidden');
  Plotly.newPlot('forecastChart', [], baseLayout({
    margin: { l: 65, r: 20, t: 20, b: 60 },
    xaxis: xAxis({ title: { text: 'Run a forecast to see results ⚡', font: { size: 13, color: C.label } } }),
    yaxis: yAxis({ title: { text: 'MCP (₹/MWh)', font: { size: 12 } } }),
  }), PLOTLY_CFG);
}

// ── Peak / Low detection ───────────────────────────────────────────────────
/**
 * findExtremes — returns indices of top-N highest and bottom-N lowest prices.
 * Uses a min-separation guard (minGap) so nearby spikes don't all cluster together.
 */
function findExtremes(prices, n = 5, minGap = 8) {
  const indexed = prices.map((v, i) => ({ v, i }));

  const pickTop = (arr, descending) => {
    const sorted = [...arr].sort((a, b) => descending ? b.v - a.v : a.v - b.v);
    const chosen = [];
    for (const pt of sorted) {
      if (chosen.every(c => Math.abs(c.i - pt.i) >= minGap)) {
        chosen.push(pt);
        if (chosen.length === n) break;
      }
    }
    return chosen;
  };

  return {
    highs: pickTop(indexed, true),
    lows:  pickTop(indexed, false),
  };
}

// ── Moving average helper ──────────────────────────────────────────────────
function computeMA(prices, win) {
  return prices.map((_, i, arr) => {
    const slice = arr.slice(Math.max(0, i - win + 1), i + 1);
    return slice.reduce((s, v) => s + v, 0) / slice.length;
  });
}

// ── Main render ───────────────────────────────────────────────────────────
function renderMainChart(d) {
  // If compare mode is on and we have enough days, render comparison instead
  if (S.compareMode && d.daily_avg && d.daily_avg.length >= 2) {
    renderCompareChart(d);
    return;
  }

  const times  = d.times;
  const prices = d.prices;

  // Moving average window: 24 pts (6 h at 15-min), or 25% of data if shorter
  const MA_WIN = Math.min(24, Math.max(4, Math.floor(prices.length / 4)));
  const ma     = computeMA(prices, MA_WIN);

  // ── Percent-change series (for hover panel, stored as customdata) ─────────
  // pctChange[i] = (prices[i] - prices[i-1]) / prices[i-1] * 100
  const pctChange = prices.map((v, i) =>
    i === 0 ? 0 : +((v - prices[i-1]) / prices[i-1] * 100).toFixed(2)
  );

  // ── Detect peak & low points ──────────────────────────────────────────────
  const { highs, lows } = findExtremes(prices, 5);

  // ── Build traces ──────────────────────────────────────────────────────────
  const traces = [
    // 1. Gradient fill area (invisible line, fill to zero)
    {
      type: 'scatter', mode: 'none',
      x: times, y: prices,
      fill: 'tozeroy',
      fillcolor: 'rgba(99,102,241,0.07)',
      hoverinfo: 'skip',
      showlegend: false,
      name: '_fill',
    },

    // 2. Main forecast line — spline, stores [ma, pct] in customdata for hover
    {
      type: 'scatter', mode: 'lines',
      name: 'MCP Forecast',
      x: times, y: prices,
      customdata: prices.map((_, i) => [
        Math.round(ma[i]),
        pctChange[i] >= 0 ? '+' + pctChange[i] : String(pctChange[i]),
      ]),
      line: { color: C.accent, width: 2.2, shape: 'spline', smoothing: 0.8 },
      hovertemplate:
        '<b>Time:</b> %{x}<br>' +
        '<b>MCP:</b> ₹%{y:,.0f}/MWh<br>' +
        '<b>MA:</b> ₹%{customdata[0]:,}<br>' +
        '<b>Δ:</b> %{customdata[1]}%' +
        '<extra>Forecast</extra>',
    },

    // 3. Moving average — dashed amber spline
    {
      type: 'scatter', mode: 'lines',
      name: `${MA_WIN}-pt MA`,
      x: times, y: ma,
      line: { color: C.ma, width: 1.5, dash: 'dot', shape: 'spline', smoothing: 0.6 },
      hoverinfo: 'skip',   // shown via customdata in trace 2
    },

    // 4. Peak markers — red glow dots with "Peak ₹XXXX" labels
    {
      type: 'scatter', mode: 'markers+text',
      name: 'Peak',
      x: highs.map(p => times[p.i]),
      y: highs.map(p => p.v),
      text: highs.map(p => `▲ ₹${Math.round(p.v).toLocaleString('en-IN')}`),
      textposition: 'top center',
      textfont: { color: C.red, size: 10, family: C.font },
      marker: {
        color:   'rgba(239,68,68,0.9)',
        size:    9,
        symbol:  'circle',
        line:    { color: 'rgba(239,68,68,0.4)', width: 6 },  // glow ring
      },
      hovertemplate: '<b>Peak</b><br>%{x}<br>₹%{y:,.0f}/MWh<extra></extra>',
    },

    // 5. Low markers — green dots with "Low ₹XXXX" labels
    {
      type: 'scatter', mode: 'markers+text',
      name: 'Low',
      x: lows.map(p => times[p.i]),
      y: lows.map(p => p.v),
      text: lows.map(p => `▼ ₹${Math.round(p.v).toLocaleString('en-IN')}`),
      textposition: 'bottom center',
      textfont: { color: C.green, size: 10, family: C.font },
      marker: {
        color:   'rgba(16,185,129,0.9)',
        size:    9,
        symbol:  'circle',
        line:    { color: 'rgba(16,185,129,0.35)', width: 6 },
      },
      hovertemplate: '<b>Low</b><br>%{x}<br>₹%{y:,.0f}/MWh<extra></extra>',
    },
  ];

  // ── Layout ─────────────────────────────────────────────────────────────────
  const layout = baseLayout({
    margin: { l: 65, r: 20, t: 40, b: 60 },
    xaxis: xAxis({
      rangeslider: {
        visible: true, thickness: 0.05,
        bgcolor: 'rgba(255,255,255,0.02)',
        bordercolor: 'rgba(255,255,255,0.04)',
      },
      rangeselector: {
        // 6H and 12H added; times are strings so we use 'hour' step
        buttons: [
          { count: 6,  label: '6H',  step: 'hour', stepmode: 'backward' },
          { count: 12, label: '12H', step: 'hour', stepmode: 'backward' },
          { count: 1,  label: '1D',  step: 'day',  stepmode: 'backward' },
          { count: 7,  label: '1W',  step: 'day',  stepmode: 'backward' },
          { step: 'all', label: 'All' },
        ],
        bgcolor:     'rgba(255,255,255,0.04)',
        activecolor: 'rgba(99,102,241,0.35)',
        bordercolor: 'rgba(255,255,255,0.07)',
        font:        { color: C.label, size: 11 },
        x: 0, y: 1.08,
      },
    }),
    yaxis: yAxis({ title: { text: 'MCP (₹/MWh)', font: { size: 12, color: C.label } } }),
    shapes: [{
      // Mean reference line
      type: 'line', x0: 0, x1: 1, xref: 'paper',
      y0: d.summary?.mean, y1: d.summary?.mean, yref: 'y',
      line: { color: 'rgba(255,255,255,0.08)', width: 1, dash: 'dash' },
    }],
    annotations: [{
      x: 1, xref: 'paper', y: d.summary?.mean, yref: 'y',
      text: `Mean ₹${Math.round(d.summary?.mean || 0).toLocaleString('en-IN')}`,
      showarrow: false, xanchor: 'right', yanchor: 'bottom',
      font: { color: 'rgba(255,255,255,0.3)', size: 9 },
    }],
  });

  Plotly.react('forecastChart', traces, layout, PLOTLY_CFG);

  // ── Wire hover panel AFTER chart is rendered ──────────────────────────────
  wireHoverPanel('forecastChart', ma, pctChange);
}

/* ──────────────────────────────────────────────────────────────────────
   FEATURE 1 — LIVE HOVER PANEL
   Listens to Plotly's plotly_hover event on the main chart.
   Updates a fixed DOM panel (top-right) with time, MCP, MA, Δ%.
────────────────────────────────────────────────────────────────────── */

/**
 * wireHoverPanel — attach plotly_hover / plotly_unhover to forecastChart.
 * Called once after every renderMainChart().
 * @param {string} divId   - Plotly div id
 * @param {number[]} ma    - pre-computed MA array (same length as prices)
 * @param {number[]} pct   - pre-computed % change array
 */
function wireHoverPanel(divId, ma, pct) {
  const el = document.getElementById('hoverPanel');
  if (!el) return;

  const chartDiv = document.getElementById(divId);
  if (!chartDiv) return;

  // Remove previous listeners by cloning the node (cleanest approach)
  chartDiv.removeAllListeners && chartDiv.removeAllListeners('plotly_hover');
  chartDiv.removeAllListeners && chartDiv.removeAllListeners('plotly_unhover');

  chartDiv.on('plotly_hover', function (eventData) {
    // Only react to the main forecast trace (index 1 = the line trace)
    const pts = eventData.points;
    if (!pts || pts.length === 0) return;

    // Find the forecast-line point (trace index 1)
    const pt = pts.find(p => p.curveNumber === 1) || pts[0];
    const i  = pt.pointIndex;

    const time  = pt.x;
    const price = pt.y;
    const maVal = ma[i] != null ? Math.round(ma[i]).toLocaleString('en-IN') : '—';
    const delta = pct[i];
    const sign  = delta >= 0 ? '+' : '';
    const deltaColor = delta >= 0 ? '#10b981' : '#ef4444';

    // Populate panel fields
    el.querySelector('#hpTime').textContent  = time || '—';
    el.querySelector('#hpPrice').textContent = price != null ? '₹' + Math.round(price).toLocaleString('en-IN') : '—';
    el.querySelector('#hpMA').textContent    = '₹' + maVal;
    const deltaEl = el.querySelector('#hpDelta');
    deltaEl.textContent = sign + delta + '%';
    deltaEl.style.color = deltaColor;

    // Show panel
    el.classList.remove('hp-hidden');
    el.classList.add('hp-visible');
  });

  chartDiv.on('plotly_unhover', function () {
    el.classList.remove('hp-visible');
    el.classList.add('hp-hidden');
  });
}

/* ──────────────────────────────────────────────────────────────────────
   FEATURE 5 — MULTI-DAY COMPARISON CHART
   Groups data by day, plots one spline line per day.
   Each line gets a distinct colour from the palette.
   Triggered by the "Compare Days" toggle button.
────────────────────────────────────────────────────────────────────── */

// Colour palette for comparison lines (distinct, works on dark bg)
const COMPARE_COLORS = [
  '#6366f1', '#f59e0b', '#10b981', '#ec4899',
  '#06b6d4', '#f97316', '#a78bfa', '#34d399',
];

function toggleCompareMode() {
  if (!S.lastData) return;
  S.compareMode = !S.compareMode;
  updateCompareBtn();
  renderMainChart(S.lastData);
}

function updateCompareBtn() {
  const btn = document.getElementById('compareDaysBtn');
  if (!btn) return;
  btn.classList.toggle('ca-btn-active', S.compareMode);
  btn.textContent = S.compareMode ? '✕ Single View' : '⇄ Compare Days';
}

/**
 * renderCompareChart — one line per day, x-axis = hour (0-23).
 * Lines are overlaid so you can see how each day's intra-day pattern differs.
 */
function renderCompareChart(d) {
  const prices    = d.prices;
  const dayLabels = d.daily_labels || [];
  const n         = Math.min(dayLabels.length, 8); // cap at 8 days (colour palette)
  if (n < 2) { renderMainChart(d); return; }

  const bpd   = Math.round(prices.length / n);     // blocks per day
  const hours = Array.from({ length: Math.min(bpd, 96) }, (_, i) =>
    `${String(Math.floor(i * 15 / 60)).padStart(2,'0')}:${String(i * 15 % 60).padStart(2,'0')}`
  );

  const traces = dayLabels.slice(0, n).map((label, day) => {
    const start = day * bpd;
    const slice = prices.slice(start, Math.min(start + bpd, prices.length));
    return {
      type: 'scatter', mode: 'lines',
      name: label,
      x: hours.slice(0, slice.length),
      y: slice,
      line: {
        color:     COMPARE_COLORS[day % COMPARE_COLORS.length],
        width:     1.8,
        shape:     'spline',
        smoothing: 0.7,
      },
      hovertemplate: `<b>${label}</b> %{x}<br>₹%{y:,.0f}/MWh<extra></extra>`,
    };
  });

  const layout = baseLayout({
    margin: { l: 65, r: 20, t: 30, b: 60 },
    xaxis: xAxis({ title: { text: 'Time of Day (HH:MM)', font: { size: 12 } } }),
    yaxis: yAxis({ title: { text: 'MCP (₹/MWh)', font: { size: 12 } } }),
    legend: {
      orientation: 'h', yanchor: 'bottom', y: 1.01,
      xanchor: 'right', x: 1,
      bgcolor: 'rgba(0,0,0,0)',
      font: { size: 10, color: C.label },
    },
  });

  Plotly.react('forecastChart', traces, layout, PLOTLY_CFG);
}

/* ══════════════════════════════════════════════════════════════════════
   CHART 2 — CANDLESTICK (OHLC per day)
   Converts 15-min block data → daily O/H/L/C
══════════════════════════════════════════════════════════════════════ */

/**
 * buildOHLC — convert flat prices[] + daily_labels[] into OHLC arrays.
 * We split prices into groups of 96 (one full day) and extract:
 *   Open  = prices[0]  of the day
 *   High  = max(prices) of the day
 *   Low   = min(prices) of the day
 *   Close = prices[95] of the day  (or last if < 96 blocks)
 */
function buildOHLC(d) {
  const prices = d.prices;
  const labels = d.daily_labels || [];
  const n      = labels.length;
  const bpd    = Math.round(prices.length / Math.max(n, 1)); // blocks per day

  const ohlc = { dates: [], open: [], high: [], low: [], close: [] };

  for (let day = 0; day < n; day++) {
    const start  = day * bpd;
    const end    = Math.min(start + bpd, prices.length);
    const slice  = prices.slice(start, end);
    if (slice.length === 0) continue;

    ohlc.dates.push(labels[day]);
    ohlc.open.push(slice[0]);
    ohlc.high.push(Math.max(...slice));
    ohlc.low.push(Math.min(...slice));
    ohlc.close.push(slice[slice.length - 1]);
  }
  return ohlc;
}

function renderCandleChart(d) {
  const ohlc = buildOHLC(d);
  if (ohlc.dates.length === 0) return;

  const trace = {
    type: 'candlestick',
    name: 'MCP OHLC',
    x:     ohlc.dates,
    open:  ohlc.open,
    high:  ohlc.high,
    low:   ohlc.low,
    close: ohlc.close,
    increasing: {
      line:      { color: C.green, width: 1.5 },
      fillcolor: 'rgba(16,185,129,0.5)',
    },
    decreasing: {
      line:      { color: C.red, width: 1.5 },
      fillcolor: 'rgba(239,68,68,0.5)',
    },
    whiskerwidth: 0.3,
    hovertemplate:
      '<b>%{x}</b><br>' +
      'Open:  ₹%{open:,.0f}<br>' +
      'High:  ₹%{high:,.0f}<br>' +
      'Low:   ₹%{low:,.0f}<br>' +
      'Close: ₹%{close:,.0f}' +
      '<extra>OHLC</extra>',
  };

  const layout = baseLayout({
    margin: { l: 65, r: 20, t: 16, b: 60 },
    xaxis: xAxis({
      rangeslider: { visible: false },   // cleaner without slider on candle
      type: 'category',
    }),
    yaxis: yAxis({ title: { text: 'MCP (₹/MWh)', font: { size: 12 } } }),
  });

  Plotly.react('candleChart', [trace], layout, PLOTLY_CFG);
}

/* ══════════════════════════════════════════════════════════════════════
   CHART 3 — HEATMAP  (hour of day × calendar day)
   X-axis: hours 0–23   Y-axis: date labels   Color: avg MCP
══════════════════════════════════════════════════════════════════════ */

/**
 * buildHeatMatrix — reshape flat prices (15-min blocks) into a 2-D matrix:
 *   rows    = days  (daily_labels)
 *   columns = hours 0–23
 *   value   = average MCP across the 4 blocks in that hour
 */
function buildHeatMatrix(d) {
  const prices   = d.prices;
  const dayLabels = d.daily_labels || [];
  const nDays    = dayLabels.length;
  if (nDays === 0) return null;

  const bpd   = Math.round(prices.length / nDays);  // blocks per day (~96)
  const bph   = Math.round(bpd / 24);               // blocks per hour (~4)
  const hours = Array.from({ length: 24 }, (_, h) => `${String(h).padStart(2,'0')}:00`);
  const z     = [];   // [nDays][24]

  for (let day = 0; day < nDays; day++) {
    const row = [];
    for (let h = 0; h < 24; h++) {
      const start = day * bpd + h * bph;
      const end   = Math.min(start + bph, prices.length);
      const slice = prices.slice(start, end);
      const avg   = slice.length ? slice.reduce((a, b) => a + b, 0) / slice.length : null;
      row.push(avg != null ? Math.round(avg) : null);
    }
    z.push(row);
  }

  return { z, x: hours, y: dayLabels };
}

function renderHeatmap(d) {
  const mat = buildHeatMatrix(d);
  if (!mat) return;

  const dynamicHeight = Math.max(260, Math.min(mat.y.length * 28, 600));
  document.getElementById('heatChart').style.height = dynamicHeight + 'px';

  const trace = {
    type: 'heatmap',
    name: 'MCP Heatmap',
    z:    mat.z,
    x:    mat.x,
    y:    mat.y,
    colorscale:  'Plasma',   // dark → bright = low → high price
    reversescale: false,
    showscale:   true,
    colorbar: {
      title:       { text: '₹/MWh', font: { size: 11, color: C.label } },
      tickfont:    { color: C.label, size: 10 },
      tickprefix:  '₹',
      thickness:   14,
      len:         0.9,
      bgcolor:     'rgba(0,0,0,0)',
      bordercolor: 'rgba(255,255,255,0.05)',
    },
    hovertemplate:
      '<b>%{y}  %{x}</b><br>' +
      'Avg MCP: ₹%{z:,.0f}/MWh' +
      '<extra></extra>',
    xgap: 1,
    ygap: 1,
  };

  const layout = {
    paper_bgcolor: C.bg,
    plot_bgcolor:  C.bg,
    font:   { family: C.font, color: C.label, size: 11 },
    margin: { l: 80, r: 80, t: 10, b: 50 },
    xaxis: {
      title:    { text: 'Hour of Day', font: { size: 12, color: C.label } },
      tickfont: { color: C.tick, size: 10 },
      gridcolor: 'rgba(0,0,0,0)',
    },
    yaxis: {
      title:     { text: 'Date', font: { size: 12, color: C.label } },
      tickfont:  { color: C.tick, size: 10 },
      autorange: 'reversed',   // most recent day at top
      gridcolor: 'rgba(0,0,0,0)',
    },
  };

  Plotly.react('heatChart', [trace], layout, { ...PLOTLY_CFG, modeBarButtonsToRemove: ['lasso2d','select2d'] });
}

/* ══════════════════════════════════════════════════════════════════════
   CHART 4 — DAILY AVERAGE BAR CHART
   Gradient colour: low price → indigo, high price → amber
══════════════════════════════════════════════════════════════════════ */
function renderDailyChart(d) {
  if (!d.daily_avg || d.daily_avg.length === 0) return;

  const vals   = d.daily_avg;
  const labels = d.daily_labels || vals.map((_, i) => `Day ${i+1}`);

  const mn = Math.min(...vals);
  const mx = Math.max(...vals);

  // Map each bar value to a colour on the indigo→amber gradient
  const barColors = vals.map(v => {
    const t = (v - mn) / (mx - mn || 1);
    // Interpolate: #6366f1 (indigo) → #f59e0b (amber)
    const r = Math.round(99  + t * (245 - 99));
    const g = Math.round(102 + t * (158 - 102));
    const b = Math.round(241 + t * (11  - 241));
    return `rgba(${r},${g},${b},0.85)`;
  });

  const trace = {
    type: 'bar',
    name: 'Daily Avg MCP',
    x:    labels,
    y:    vals,
    marker: {
      color:        barColors,
      cornerradius: 5,
      line: { color: 'rgba(255,255,255,0.04)', width: 0.5 },
    },
    hovertemplate: '<b>%{x}</b><br>Avg MCP: ₹%{y:,.0f}/MWh<extra></extra>',
    // Animate on load
    textposition: 'none',
  };

  const layout = baseLayout({
    margin:     { l: 65, r: 20, t: 10, b: 60 },
    hovermode:  'closest',
    showlegend: false,
    xaxis: {
      gridcolor: 'rgba(0,0,0,0)',
      tickfont:  { color: C.tick, family: C.font, size: 11 },
    },
    yaxis: yAxis({ title: { text: 'Avg MCP (₹/MWh)', font: { size: 12 } } }),
    // Add a horizontal mean reference line
    shapes: [{
      type: 'line', x0: 0, x1: 1, xref: 'paper',
      y0: d.summary?.mean, y1: d.summary?.mean, yref: 'y',
      line: { color: C.ma, width: 1.2, dash: 'dash' },
    }],
    annotations: [{
      x: 1, y: d.summary?.mean, xref: 'paper', yref: 'y',
      text: `Avg ₹${Math.round(d.summary?.mean || 0).toLocaleString('en-IN')}`,
      showarrow: false,
      xanchor: 'right', yanchor: 'bottom',
      font: { color: C.ma, size: 10 },
    }],
  });

  Plotly.react('dailyChart', [trace], layout, PLOTLY_CFG);
}

/* ──────────────────────────────────────────────────────────────────────
   CHART UTILITIES
────────────────────────────────────────────────────────────────────── */
function resetZoom(divId) {
  Plotly.relayout(divId, { 'xaxis.autorange': true, 'yaxis.autorange': true });
}
function saveChart(divId, filename) {
  Plotly.downloadImage(divId, { format: 'png', filename, width: 1600, height: 700 });
}

/* ──────────────────────────────────────────────────────────────────────
   TABLE
────────────────────────────────────────────────────────────────────── */
function renderTable() {
  const data  = S.filtered;
  const total = data.length;
  const pages = Math.ceil(total / S.rowsPerPage);
  const start = (S.page - 1) * S.rowsPerPage;
  const slice = data.slice(start, start + S.rowsPerPage);

  document.getElementById('tBody').innerHTML = slice.map(r => `
    <tr>
      <td class="td-ts">${r.ts}</td>
      <td class="td-price">₹${Number(r.price).toLocaleString('en-IN', { minimumFractionDigits: 2 })}</td>
      <td class="td-dim">${r.day}</td>
      <td class="td-dim">${r.hour}</td>
    </tr>`).join('');

  document.getElementById('rowInfo').textContent =
    `Showing ${start + 1}–${Math.min(start + S.rowsPerPage, total)} of ${total}`;

  buildPaginator(pages);
}

function buildPaginator(pages) {
  if (pages <= 1) { document.getElementById('paginator').innerHTML = ''; return; }
  const cur = S.page;
  let h = '';
  h += pgBtn('‹', cur > 1 ? cur - 1 : null);
  if (cur > 3) h += pgBtn('1', 1) + (cur > 4 ? '<span style="color:var(--txt3);padding:0 4px">…</span>' : '');
  for (let p = Math.max(1, cur-2); p <= Math.min(pages, cur+2); p++) h += pgBtn(p, p, p === cur);
  if (cur < pages-2) h += (cur < pages-3 ? '<span style="color:var(--txt3);padding:0 4px">…</span>' : '') + pgBtn(pages, pages);
  h += pgBtn('›', cur < pages ? cur+1 : null);
  document.getElementById('paginator').innerHTML = h;
}
function pgBtn(label, page, active = false) {
  if (page == null) return `<button class="pg-btn" disabled>${label}</button>`;
  return `<button class="pg-btn ${active?'active':''}" onclick="gotoPage(${page})">${label}</button>`;
}
function gotoPage(p) { S.page = p; renderTable(); }

function filterTable() {
  const q = (document.getElementById('searchBox').value || '').toLowerCase();
  S.filtered = S.forecastData.filter(r => r.ts.toLowerCase().includes(q));
  S.page = 1;
  renderTable();
}
function sortTable(col) {
  S.sortAsc = S.sortCol === col ? !S.sortAsc : true;
  S.sortCol = col;
  const keys = ['ts', 'price', 'day', 'hour'];
  const k    = keys[col];
  S.filtered.sort((a, b) => {
    const av = a[k] ?? '', bv = b[k] ?? '';
    return S.sortAsc ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1);
  });
  renderTable();
}

/* ──────────────────────────────────────────────────────────────────────
   DOWNLOAD
────────────────────────────────────────────────────────────────────── */
function downloadForecast() {
  const h = S.mode === 'custom' ? 'weekly' : S.horizon;
  window.location = '/api/download_forecast/' + h;
}

/* ──────────────────────────────────────────────────────────────────────
   UI HELPERS
────────────────────────────────────────────────────────────────────── */
function setLoading(on, msg) {
  const btnId  = S.mode === 'custom' ? 'runCustomBtn' : 'runHorizonBtn';
  const btn     = document.getElementById(btnId);
  const spinner = document.getElementById('chartSpinner');
  const smsg    = document.getElementById('spinnerMsg');
  if (btn) btn.disabled = on;
  if (spinner) spinner.classList.toggle('hidden', !on);
  if (smsg && msg) smsg.textContent = msg;
}
function handleError(msg, detail) {
  setLoading(false);
  showAlert('err', '❌ ' + msg);
  if (detail) console.error('[PowerCast]', detail);
}
function showAlert(type, msg) {
  const el = document.getElementById('alert');
  el.className = 'alert alert-' + (type === 'ok' ? 'ok' : 'err');
  document.getElementById('alertMsg').textContent = msg;
}
function hideAlert() {
  document.getElementById('alert').classList.add('hidden');
}
function clearResults() {
  ['candleCard','heatCard','dailyCard','tableCard'].forEach(id => hide(id));
  // Hide live hover panel
  const hp = document.getElementById('hoverPanel');
  if (hp) { hp.classList.remove('hp-visible'); hp.classList.add('hp-hidden'); }
  S.forecastData = [];
  S.filtered     = [];
}
function show(id) { document.getElementById(id)?.classList.remove('hidden'); }
function hide(id) { document.getElementById(id)?.classList.add('hidden'); }
function safeJSON(t) { try { return JSON.parse(t); } catch (_) { return null; } }
function fmtDate(d)  { return d.toISOString().split('T')[0]; }
