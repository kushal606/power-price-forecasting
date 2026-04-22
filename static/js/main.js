/* ═══════════════════════════════════════════════════════════
   PowerCast — Shared JS  (main.js)
   Loaded on every page via base.html
═══════════════════════════════════════════════════════════ */

'use strict';

// ── Clock ──────────────────────────────────────────────────
(function startClock() {
  const el = document.getElementById('clock');
  if (!el) return;
  const tick = () => {
    el.textContent = new Date().toLocaleString('en-IN', {
      day: '2-digit', month: 'short', year: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false,
    });
  };
  tick();
  setInterval(tick, 1000);
})();

// ── Model status pill ──────────────────────────────────────
(function checkModelStatus() {
  const dot  = document.getElementById('pillDot');
  const txt  = document.getElementById('pillText');
  if (!dot || !txt) return;

  fetch('/api/test')
    .then(r => r.json())
    .then(d => {
      if (d.model_ready) {
        dot.classList.add('ready');
        dot.classList.remove('pulse');
        txt.textContent = 'Model Ready';
      } else {
        dot.classList.add('error');
        dot.classList.remove('pulse');
        txt.textContent = d.model_error ? 'Model Error' : 'Loading…';
        // Keep checking until ready
        if (!d.model_ready && !d.model_error) {
          setTimeout(checkModelStatus, 4000);
        }
      }
    })
    .catch(() => {
      dot.classList.add('error');
      txt.textContent = 'Offline';
    });
})();

// ── Theme ──────────────────────────────────────────────────
function toggleTheme() {
  const html  = document.documentElement;
  const next  = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  localStorage.setItem('pc-theme', next);
  document.getElementById('themeIco').textContent = next === 'dark' ? '☀️' : '🌙';
  document.getElementById('themeLbl').textContent = next === 'dark' ? 'Light' : 'Dark';
}

// Restore saved theme
(function restoreTheme() {
  const saved = localStorage.getItem('pc-theme');
  if (saved && saved !== 'dark') {
    document.documentElement.setAttribute('data-theme', saved);
    const ico = document.getElementById('themeIco');
    const lbl = document.getElementById('themeLbl');
    if (ico) ico.textContent = '🌙';
    if (lbl) lbl.textContent = 'Dark';
  }
})();

// ── Sidebar (mobile) ───────────────────────────────────────
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebarVeil').classList.toggle('open');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebarVeil').classList.remove('open');
}
