/* =============================================
   Claude 株式取引シミュレーター – フロントエンド
   ============================================= */

'use strict';

// ─── グローバル状態 ───────────────────────────
let equityChart  = null;
let pnlChart     = null;
let allSessions  = [];
let currentRunId = null;
let sseSource    = null;

// ─── 初期化 ──────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initRunForm();
  loadDefaults();
  loadDashboard();
  loadPortfolio();

  // シグナルタブを開いたら自動で最新シグナルを取得・生成
  document.querySelectorAll('.tab').forEach(btn => {
    if (btn.dataset.tab === 'signal') {
      btn.addEventListener('click', () => autoLoadSignal());
    }
  });

  // 30秒ごとにダッシュボードを自動更新
  setInterval(() => {
    if (!document.querySelector('#panel-dashboard').classList.contains('active')) return;
    loadDashboard();
  }, 30_000);
});

// ─── タブ切り替え ────────────────────────────
function initTabs() {
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `panel-${name}`));

  if (name === 'dashboard') loadDashboard();
  if (name === 'trades')    populateSessionSelects();
  if (name === 'analysis')  populateSessionSelects();
}

// ─── デフォルト設定の読み込み ─────────────────
async function loadDefaults() {
  try {
    const res  = await fetch('/api/config/defaults');
    const cfg  = await res.json();
    document.getElementById('start-date').value = cfg.start_date || '2022-01-01';
    document.getElementById('end-date').value   = cfg.end_date   || '2024-12-31';
    buildParamGrid(cfg.strategy_params || {});
  } catch (e) {
    console.warn('デフォルト設定の読み込み失敗:', e);
  }
}

// ─── パラメータグリッドの動的生成 ──────────────
const PARAM_LABELS = {
  ema_fast:          '短期EMA（日）',
  ema_slow:          '長期EMA（日）',
  ema_trend:         'トレンドEMA（日）',
  rsi_period:        'RSI期間',
  rsi_lower:         'RSI 下限',
  rsi_upper:         'RSI 上限',
  macd_fast:         'MACD 短期',
  macd_slow:         'MACD 長期',
  macd_signal:       'MACDシグナル',
  momentum_period:   'モメンタム期間',
  momentum_skip:     'モメンタムスキップ',
  top_n_momentum:    'モメンタム上位N',
  volume_ma_period:  'ボリュームMA期間',
  volume_multiplier: 'ボリューム倍率',
  min_score:         '買いスコア閾値(1-5)',
};

function buildParamGrid(params) {
  const grid = document.getElementById('param-grid');
  grid.innerHTML = '';
  for (const [key, val] of Object.entries(params)) {
    const isFloat = key === 'volume_multiplier';
    const div = document.createElement('div');
    div.className = 'param-item';
    div.innerHTML = `
      <label for="p_${key}">${PARAM_LABELS[key] || key}</label>
      <input type="number" id="p_${key}" name="${key}"
             value="${val}" step="${isFloat ? 0.1 : 1}" min="1" />
    `;
    grid.appendChild(div);
  }
}

function readParams() {
  const params = {};
  document.querySelectorAll('#param-grid input').forEach(inp => {
    params[inp.name] = inp.name === 'volume_multiplier'
      ? parseFloat(inp.value)
      : parseInt(inp.value, 10);
  });
  return params;
}

// ─── バックテスト実行フォーム ──────────────────
function initRunForm() {
  // モード切替で「反復回数」欄を表示/非表示
  document.querySelectorAll('input[name="mode"]').forEach(r => {
    r.addEventListener('change', () => {
      const itGroup = document.getElementById('iterations-group');
      itGroup.style.display = r.value === 'iterative' ? '' : 'none';
    });
  });

  document.getElementById('run-form').addEventListener('submit', async e => {
    e.preventDefault();
    await startBacktest();
  });
}

async function startBacktest() {
  const mode       = document.querySelector('input[name="mode"]:checked').value;
  const start_date = document.getElementById('start-date').value;
  const end_date   = document.getElementById('end-date').value;
  const iterations = parseInt(document.getElementById('iterations').value, 10);

  const payload = {
    mode, start_date, end_date, iterations,
    strategy_params: readParams(),
  };

  // UI をロック
  const btn = document.getElementById('run-btn');
  btn.disabled = true;
  document.getElementById('run-btn-text').textContent = '⏳ 実行中...';
  document.getElementById('cancel-btn').classList.remove('hidden');
  document.getElementById('result-preview').classList.add('hidden');
  clearLog();
  setProgress(0, '接続中...');

  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json();
      appendLog(`エラー: ${err.error || res.statusText}`, 'log-error');
      unlockRunBtn();
      return;
    }
    appendLog(`バックテスト開始 (${mode}) — SSEで進捗を受信中...`, 'log-info');
    listenSSE();
  } catch (err) {
    appendLog(`接続エラー: ${err}`, 'log-error');
    unlockRunBtn();
  }
}

// ─── Server-Sent Events で進捗受信 ────────────
function listenSSE() {
  if (sseSource) { sseSource.close(); sseSource = null; }

  sseSource = new EventSource('/api/events');

  sseSource.onmessage = e => {
    if (!e.data.trim() || e.data === '{}') return;
    const ev = JSON.parse(e.data);

    if (ev.progress >= 0) setProgress(ev.progress, ev.message || '');
    if (ev.message)       appendLog(ev.message, ev.error ? 'log-error' : 'log-info');

    if (ev.done) {
      sseSource.close();
      sseSource = null;
      onRunComplete();
    }
  };

  sseSource.onerror = () => {
    sseSource.close();
    sseSource = null;
    // バックテスト中ならポーリングにフォールバック
    pollStatus();
  };
}

// SSEが失敗した場合のポーリングフォールバック
async function pollStatus() {
  while (true) {
    await sleep(2000);
    try {
      const res = await fetch('/api/status');
      const st  = await res.json();
      setProgress(st.progress, st.message);
      if (!st.is_running) { onRunComplete(); break; }
    } catch { break; }
  }
}

async function onRunComplete() {
  setProgress(100, '完了');
  appendLog('✓ バックテスト完了', 'log-ok');
  unlockRunBtn();

  // 最新セッションのサマリーを速報表示
  try {
    const sessions = await (await fetch('/api/sessions')).json();
    if (sessions.length > 0) {
      const latest = sessions[0];
      showResultPreview(latest);
      currentRunId = latest.run_id;
    }
  } catch {}

  // ダッシュボード更新
  await loadDashboard();
}

function showResultPreview(s) {
  const pct  = (s.total_return_pct ?? 0);
  document.getElementById('rp-return').textContent  = fmt_pct(pct);
  document.getElementById('rp-return').className    = 'rp-val ' + (pct >= 0 ? 'positive' : 'negative');
  document.getElementById('rp-winrate').textContent = `${(s.win_rate ?? 0).toFixed(1)}%`;
  document.getElementById('rp-sharpe').textContent  = (s.sharpe_ratio ?? 0).toFixed(2);
  document.getElementById('rp-dd').textContent      = `${(s.max_drawdown_pct ?? 0).toFixed(2)}%`;
  document.getElementById('result-preview').classList.remove('hidden');
}

// ─── ダッシュボード ──────────────────────────
async function loadDashboard() {
  try {
    const [sessRes, perfRes] = await Promise.all([
      fetch('/api/sessions'),
      fetch('/api/performance'),
    ]);
    allSessions = await sessRes.json();
    const perf  = await perfRes.json();

    updateKPIs(perf);
    renderSessionsTable(allSessions);
    populateSessionSelects();

    // 最初のセッションの資産推移を自動表示
    const sel = document.getElementById('session-select');
    if (allSessions.length > 0 && !sel.value) {
      sel.value = allSessions[0].run_id;
      loadEquityChart(allSessions[0].run_id);
    }
  } catch (e) {
    console.warn('ダッシュボード読み込みエラー:', e);
  }
}

function updateKPIs(perf) {
  if (!perf || !perf.total_sessions) return;
  const avgRet = perf.avg_return_pct ?? 0;
  document.getElementById('kpi-sessions').textContent    = perf.total_sessions;
  document.getElementById('kpi-avg-return').textContent  = fmt_pct(avgRet);
  document.getElementById('kpi-avg-return').className    = 'kpi-value ' + (avgRet >= 0 ? 'positive' : 'negative');
  document.getElementById('kpi-best-return').textContent = fmt_pct(perf.best_return_pct ?? 0);
  document.getElementById('kpi-win-rate').textContent    = `${(perf.avg_win_rate ?? 0).toFixed(1)}%`;
  document.getElementById('kpi-sharpe').textContent      = (perf.avg_sharpe ?? 0).toFixed(2);
  document.getElementById('kpi-drawdown').textContent    = `${(perf.avg_max_drawdown ?? 0).toFixed(2)}%`;
}

function renderSessionsTable(sessions) {
  const tbody = document.getElementById('sessions-tbody');
  if (!sessions.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">データなし — バックテストを実行してください</td></tr>';
    return;
  }
  tbody.innerHTML = sessions.map(s => {
    const ret  = s.total_return_pct ?? 0;
    const cls  = ret >= 0 ? 'pnl-positive' : 'pnl-negative';
    const period = `${(s.start_date||'').slice(0,10)} ~ ${(s.end_date||'').slice(0,10)}`;
    return `<tr>
      <td title="${s.run_id}"><code>${s.run_id.slice(0,24)}…</code></td>
      <td>${period}</td>
      <td class="${cls}">${fmt_pct(ret)}</td>
      <td>${(s.win_rate??0).toFixed(1)}%</td>
      <td>${(s.sharpe_ratio??0).toFixed(2)}</td>
      <td class="pnl-negative">${(s.max_drawdown_pct??0).toFixed(2)}%</td>
      <td>${s.total_trades ?? '—'}</td>
      <td>
        <button class="btn-link" onclick="viewSession('${s.run_id}')">詳細</button>
      </td>
    </tr>`;
  }).join('');
}

// セッション選択 → 資産推移チャート
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('session-select').addEventListener('change', e => {
    if (e.target.value) loadEquityChart(e.target.value);
  });
});

async function loadEquityChart(runId) {
  try {
    const data = await (await fetch(`/api/equity/${runId}`)).json();
    if (!data.length) return;

    const labels   = data.map(d => d.date);
    const equities = data.map(d => d.total_equity);

    if (equityChart) equityChart.destroy();

    const ctx = document.getElementById('equity-chart').getContext('2d');
    equityChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: '資産評価額（円）',
          data: equities,
          borderColor: '#4f8ef7',
          backgroundColor: 'rgba(79,142,247,0.08)',
          borderWidth: 2,
          pointRadius: 0,
          fill: true,
          tension: 0.3,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: '#7c85a2' } },
          tooltip: {
            callbacks: {
              label: ctx => `¥${ctx.parsed.y.toLocaleString('ja-JP')}`,
            },
          },
        },
        scales: {
          x: { ticks: { color: '#7c85a2', maxTicksLimit: 12 }, grid: { color: '#2e3350' } },
          y: {
            ticks: {
              color: '#7c85a2',
              callback: v => `¥${(v/10000).toFixed(0)}万`,
            },
            grid: { color: '#2e3350' },
          },
        },
      },
    });
  } catch (e) {
    console.warn('資産推移グラフの読み込み失敗:', e);
  }
}

function viewSession(runId) {
  // ダッシュボードのセレクトを更新
  document.getElementById('session-select').value = runId;
  loadEquityChart(runId);
  switchTab('dashboard');
}

// ─── 取引履歴 ───────────────────────────────
function populateSessionSelects() {
  const selects = [
    document.getElementById('session-select'),
    document.getElementById('trades-session-select'),
    document.getElementById('analysis-session-select'),
  ];
  selects.forEach(sel => {
    const cur = sel.value;
    const opts = allSessions.map(s =>
      `<option value="${s.run_id}" ${s.run_id === cur ? 'selected' : ''}>${s.run_id.slice(-20)} (${(s.start_date||'').slice(0,10)})</option>`
    ).join('');
    sel.innerHTML = `<option value="">セッションを選択...</option>${opts}`;
    if (cur) sel.value = cur;
  });
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('trades-session-select').addEventListener('change', e => {
    if (e.target.value) loadTrades(e.target.value);
  });
  document.getElementById('trades-filter').addEventListener('input', filterTrades);
  document.getElementById('analysis-session-select').addEventListener('change', e => {
    if (e.target.value) loadAnalysis(e.target.value);
  });
});

let allTrades = [];

async function loadTrades(runId) {
  try {
    allTrades = await (await fetch(`/api/trades/${runId}`)).json();
    renderTradesTable(allTrades);
    updateTradeStats(allTrades);
    renderPnlChart(allTrades);
  } catch (e) {
    console.warn('取引履歴読み込みエラー:', e);
  }
}

function filterTrades() {
  const q = document.getElementById('trades-filter').value.trim().toUpperCase();
  const filtered = q ? allTrades.filter(t => (t.ticker || '').includes(q)) : allTrades;
  renderTradesTable(filtered);
}

function renderTradesTable(trades) {
  const tbody = document.getElementById('trades-tbody');
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty">取引データなし</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const pnlCls = t.pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
    return `<tr>
      <td><strong>${t.ticker}</strong></td>
      <td>${t.entry_date}</td>
      <td>${t.exit_date}</td>
      <td>¥${fmtNum(t.entry_price)}</td>
      <td>¥${fmtNum(t.exit_price)}</td>
      <td>${fmtNum(t.shares)}</td>
      <td class="${pnlCls}">¥${fmtNum(t.pnl, true)}</td>
      <td class="${pnlCls}">${(t.pnl_pct??0).toFixed(2)}%</td>
      <td>${t.hold_days}日</td>
      <td>${t.exit_reason || '—'}</td>
    </tr>`;
  }).join('');
}

function updateTradeStats(trades) {
  const statsEl = document.getElementById('trade-stats');
  if (!trades.length) { statsEl.style.display = 'none'; return; }
  statsEl.style.display = 'flex';

  const wins    = trades.filter(t => t.pnl > 0);
  const losses  = trades.filter(t => t.pnl <= 0);
  const totalPnl = trades.reduce((s, t) => s + (t.pnl || 0), 0);
  const avgHold  = trades.reduce((s, t) => s + (t.hold_days || 0), 0) / trades.length;

  document.getElementById('ts-total').textContent  = trades.length;
  document.getElementById('ts-wins').textContent   = wins.length;
  document.getElementById('ts-losses').textContent = losses.length;
  const pnlEl = document.getElementById('ts-pnl');
  pnlEl.textContent = `¥${fmtNum(totalPnl, true)}`;
  pnlEl.className = 'ts-val ' + (totalPnl >= 0 ? 'positive' : 'negative');
  document.getElementById('ts-hold').textContent   = `${avgHold.toFixed(1)}日`;
}

function renderPnlChart(trades) {
  const wrap = document.getElementById('pnl-chart-wrap');
  if (!trades.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';

  // 銘柄別合計損益
  const byTicker = {};
  trades.forEach(t => {
    byTicker[t.ticker] = (byTicker[t.ticker] || 0) + (t.pnl || 0);
  });
  const sorted = Object.entries(byTicker).sort((a, b) => b[1] - a[1]);
  const labels = sorted.map(x => x[0]);
  const values = sorted.map(x => x[1]);
  const colors = values.map(v => v >= 0 ? 'rgba(62,207,142,0.75)' : 'rgba(247,95,95,0.75)');

  if (pnlChart) pnlChart.destroy();
  const ctx = document.getElementById('pnl-chart').getContext('2d');
  pnlChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: '銘柄別損益（円）',
        data: values,
        backgroundColor: colors,
        borderRadius: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => `¥${ctx.parsed.y.toLocaleString('ja-JP')}` } },
      },
      scales: {
        x: { ticks: { color: '#7c85a2' }, grid: { color: '#2e3350' } },
        y: { ticks: { color: '#7c85a2', callback: v => `¥${(v/10000).toFixed(0)}万` }, grid: { color: '#2e3350' } },
      },
    },
  });
}

// ─── AI 分析 ────────────────────────────────
async function loadAnalysis(runId) {
  const placeholder = document.getElementById('analysis-placeholder');
  const content     = document.getElementById('analysis-content');

  try {
    const res = await fetch(`/api/analysis/${runId}`);
    if (!res.ok) {
      placeholder.textContent = '分析結果が見つかりません（バックテスト実行時に分析されます）';
      placeholder.classList.remove('hidden');
      content.classList.add('hidden');
      return;
    }
    const a = await res.json();
    placeholder.classList.add('hidden');
    content.classList.remove('hidden');

    // 良かった点
    renderList('good-points',             a.good_points || []);
    renderList('bad-points',              a.bad_points  || []);
    renderList('improvement-suggestions', a.improvement_suggestions || []);

    // 次回パラメータ
    const np   = a.next_strategy_params || {};
    const grid = document.getElementById('next-params-grid');
    grid.innerHTML = Object.entries(np).map(([k, v]) => `
      <div class="np-item">
        <div class="np-key">${PARAM_LABELS[k] || k}</div>
        <div class="np-val">${v}</div>
      </div>
    `).join('');

    // フルテキスト
    document.getElementById('full-analysis-text').textContent = a.full_analysis || '（テキストなし）';
  } catch (e) {
    placeholder.textContent = `読み込みエラー: ${e}`;
  }
}

function renderList(id, items) {
  const ul = document.getElementById(id);
  if (!items.length) {
    ul.innerHTML = '<li style="color:var(--text-muted)">データなし</li>';
    return;
  }
  ul.innerHTML = items.map(item => `<li>${escHtml(item)}</li>`).join('');
}

// ─── ユーティリティ ──────────────────────────
function setProgress(pct, msg) {
  document.getElementById('progress-fill').style.width = `${Math.max(0, pct)}%`;
  document.getElementById('progress-text').textContent = msg || '';
}

function appendLog(msg, cls = 'log-entry') {
  const box  = document.getElementById('log-box');
  const line = document.createElement('div');
  line.className = `log-entry ${cls}`;
  line.textContent = `[${new Date().toLocaleTimeString('ja-JP')}] ${msg}`;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function clearLog() {
  document.getElementById('log-box').innerHTML = '';
}

function unlockRunBtn() {
  const btn = document.getElementById('run-btn');
  btn.disabled = false;
  document.getElementById('run-btn-text').textContent = '▶ バックテスト開始';
  document.getElementById('cancel-btn').classList.add('hidden');
}

async function cancelRun() {
  const cancelBtn = document.getElementById('cancel-btn');
  cancelBtn.disabled = true;
  cancelBtn.textContent = 'キャンセル中...';
  try {
    await fetch('/api/cancel', { method: 'POST' });
    appendLog('キャンセルをリクエストしました。処理が停止するまでお待ちください...', 'log-error');
  } catch (e) {
    appendLog('キャンセルリクエストに失敗しました', 'log-error');
  }
}

function fmt_pct(v) {
  const sign = v >= 0 ? '+' : '';
  return `${sign}${(v ?? 0).toFixed(2)}%`;
}

function fmtNum(v, sign = false) {
  const n = Number(v ?? 0);
  const s = sign && n > 0 ? '+' : '';
  return `${s}${Math.round(n).toLocaleString('ja-JP')}`;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// ─── 最適化タブ ──────────────────────────────
let optSseSource = null;
let bestOptParams = null;

// 最適化タブが開かれたとき探索グリッドを表示
document.addEventListener('DOMContentLoaded', () => {
  document.querySelector('[data-tab="optimize"]').addEventListener('click', () => {
    loadOptDefaultGrid();
  });
});

async function loadOptDefaultGrid() {
  const grid = document.getElementById('opt-param-grid');
  if (grid.children.length > 0) return; // 既にロード済み
  try {
    const res = await fetch('/api/optimize/default_grid');
    const defaultGrid = await res.json();
    Object.entries(defaultGrid).forEach(([key, vals]) => {
      const div = document.createElement('div');
      div.className = 'param-item';
      div.innerHTML = `
        <label>${PARAM_LABELS[key] || key}</label>
        <input type="text" id="og_${key}" data-key="${key}"
               value="${vals.join(', ')}" style="font-size:12px" />
        <div style="font-size:10px;color:var(--text-muted)">カンマ区切りで複数指定</div>
      `;
      grid.appendChild(div);
    });
  } catch(e) { console.warn('グリッド読み込みエラー:', e); }
}

function readOptGrid() {
  const grid = {};
  document.querySelectorAll('#opt-param-grid input').forEach(inp => {
    const key = inp.dataset.key;
    if (!key) return;
    const vals = inp.value.split(',').map(v => {
      const n = parseFloat(v.trim());
      return isNaN(n) ? null : n;
    }).filter(v => v !== null);
    if (vals.length) grid[key] = vals;
  });
  return Object.keys(grid).length > 0 ? grid : null;
}

async function startOptimize() {
  const btn = document.getElementById('opt-run-btn');
  btn.disabled = true;
  document.getElementById('opt-btn-text').textContent = '⏳ 最適化中...';
  document.getElementById('opt-results-section').classList.add('hidden');
  optAppendLog('最適化を開始します...', 'log-info');
  setOptProgress(0, '接続中...');

  const payload = {
    start_date:       document.getElementById('opt-start').value,
    end_date:         document.getElementById('opt-end').value,
    max_combinations: parseInt(document.getElementById('opt-max-combo').value, 10),
    walk_forward:     document.getElementById('opt-walkforward').checked,
    param_grid:       readOptGrid(),
  };

  try {
    const res = await fetch('/api/optimize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json();
      optAppendLog(`エラー: ${err.error}`, 'log-error');
      unlockOptBtn(); return;
    }
    listenOptSSE();
  } catch(e) {
    optAppendLog(`接続エラー: ${e}`, 'log-error');
    unlockOptBtn();
  }
}

function listenOptSSE() {
  if (optSseSource) { optSseSource.close(); }
  optSseSource = new EventSource('/api/optimize/events');
  optSseSource.onmessage = async e => {
    if (!e.data.trim() || e.data === '{}') return;
    const ev = JSON.parse(e.data);
    if (ev.progress >= 0) setOptProgress(ev.progress, ev.message || '');
    if (ev.message)       optAppendLog(ev.message, ev.error ? 'log-error' : 'log-info');
    if (ev.done) {
      optSseSource.close();
      await onOptComplete();
    }
  };
  optSseSource.onerror = async () => {
    optSseSource.close();
    // ポーリングフォールバック
    while (true) {
      await sleep(2000);
      const st = await (await fetch('/api/optimize/status')).json();
      setOptProgress(st.progress, st.message);
      if (!st.is_running) { await onOptComplete(); break; }
    }
  };
}

async function onOptComplete() {
  unlockOptBtn();
  setOptProgress(100, '完了');
  try {
    const result = await (await fetch('/api/optimize/result')).json();
    if (result.error) { optAppendLog(`エラー: ${result.error}`, 'log-error'); return; }
    optAppendLog(`✓ 完了: ${result.total_tested}パターン評価`, 'log-ok');
    renderOptResults(result);
  } catch(e) { optAppendLog(`結果取得失敗: ${e}`, 'log-error'); }
}

function renderOptResults(result) {
  bestOptParams = result.best_params;
  document.getElementById('opt-results-section').classList.remove('hidden');

  // 最良パラメータ速報
  const bt = result.best_train || {};
  const be = result.best_test  || {};
  const isWF = result.walk_forward;
  document.getElementById('opt-best-summary').innerHTML = `
    <div class="rp-title">🥇 最良パラメータ — スコア: ${result.best_score}</div>
    <div style="font-size:12px;color:var(--text-muted);margin-bottom:10px">
      EMA ${result.best_params?.ema_fast}/${result.best_params?.ema_slow} &nbsp;|&nbsp;
      トレンドEMA ${result.best_params?.ema_trend} &nbsp;|&nbsp;
      RSI ${result.best_params?.rsi_lower}-${result.best_params?.rsi_upper} &nbsp;|&nbsp;
      ADX≥${result.best_params?.adx_threshold} &nbsp;|&nbsp;
      最小スコア ${result.best_params?.min_score}
    </div>
    <div class="rp-grid">
      <div><span class="rp-label">学習 Return</span>
           <span class="rp-val ${bt.total_return_pct>=0?'positive':'negative'}">${fmt_pct(bt.total_return_pct??0)}</span></div>
      <div><span class="rp-label">学習 勝率</span>
           <span class="rp-val">${(bt.win_rate??0).toFixed(1)}%</span></div>
      ${isWF ? `
      <div><span class="rp-label">検証 Return</span>
           <span class="rp-val ${(be.total_return_pct??0)>=0?'positive':'negative'}">${fmt_pct(be.total_return_pct??0)}</span></div>
      <div><span class="rp-label">検証 勝率</span>
           <span class="rp-val">${(be.win_rate??0).toFixed(1)}%</span></div>
      ` : ''}
    </div>
  `;

  // Top10テーブル
  const tbody = document.getElementById('opt-results-tbody');
  tbody.innerHTML = (result.top10 || []).map((r, i) => {
    const trCls = (r.train_return??0) >= 0 ? 'pnl-positive' : 'pnl-negative';
    const teCls = (r.test_return??0)  >= 0 ? 'pnl-positive' : 'pnl-negative';
    return `<tr ${i===0?'style="background:rgba(79,142,247,.08)"':''}>
      <td>${i+1}</td>
      <td><strong>${r.score}</strong></td>
      <td>${r.ema}</td>
      <td>${r.ema_trend}</td>
      <td>${r.rsi_range}</td>
      <td>${r.adx_thresh}</td>
      <td>${r.min_score}</td>
      <td class="${trCls}">${fmt_pct(r.train_return??0)}</td>
      <td>${(r.train_winrate??0).toFixed(1)}%</td>
      <td>${(r.train_sharpe??0).toFixed(2)}</td>
      <td class="${teCls}">${r.test_return != null ? fmt_pct(r.test_return) : '—'}</td>
      <td>${r.test_winrate != null ? (r.test_winrate).toFixed(1)+'%' : '—'}</td>
    </tr>`;
  }).join('');
}

function applyBestParams() {
  if (!bestOptParams) return;
  // バックテスト実行タブのパラメータフォームに反映
  switchTab('run');
  setTimeout(() => {
    Object.entries(bestOptParams).forEach(([key, val]) => {
      const inp = document.getElementById(`p_${key}`);
      if (inp) inp.value = val;
    });
    alert('最良パラメータをバックテストフォームに適用しました。\n「バックテスト開始」で実際に検証してください。');
  }, 200);
}

function setOptProgress(pct, msg) {
  document.getElementById('opt-progress-fill').style.width = `${Math.max(0, pct)}%`;
  document.getElementById('opt-progress-text').textContent = msg || '';
}

function optAppendLog(msg, cls = 'log-entry') {
  const box  = document.getElementById('opt-log-box');
  const line = document.createElement('div');
  line.className = `log-entry ${cls}`;
  line.textContent = `[${new Date().toLocaleTimeString('ja-JP')}] ${msg}`;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function unlockOptBtn() {
  const btn = document.getElementById('opt-run-btn');
  if (btn) { btn.disabled = false; document.getElementById('opt-btn-text').textContent = '🔬 最適化開始'; }
}

// =============================================
//  シグナルタブ
// =============================================

async function runSignal() {
  const market = document.getElementById('signal-market').value;
  const btn    = document.getElementById('signal-run-btn');
  btn.disabled = true;
  btn.textContent = '⏳ 生成中...';
  setSignalStatus(`${market} のシグナルを生成中です（1〜2分かかります）...`);

  try {
    const res = await fetch(`/api/run-signal?market=${market}`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    _latestSignalTs[market] = Date.now();
    setSignalStatus(`完了: 買い${data.buy_count}件 / 売り${data.sell_count}件`);
    await loadSignal();
  } catch (e) {
    setSignalStatus(`エラー: ${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ 今すぐシグナル生成';
  }
}

async function loadSignal() {
  const market = document.getElementById('signal-market').value;
  setSignalStatus('最新シグナルを取得中...');
  try {
    const res  = await fetch(`/api/signals/latest?market=${market}`);
    if (!res.ok) {
      const e = await res.json();
      setSignalStatus(e.message || 'シグナル未生成', true);
      return;
    }
    const data = await res.json();
    renderSignalResults(data);
    setSignalStatus(`${data.timestamp ? new Date(data.timestamp).toLocaleString('ja-JP') : ''} 取得`);
  } catch (e) {
    setSignalStatus(`エラー: ${e.message}`, true);
  }
}

function renderSignalResults(data) {
  document.getElementById('signal-results').classList.remove('hidden');

  // 市場レジームバナー
  const banner = document.getElementById('regime-banner');
  if (data.market_bullish) {
    banner.style.background = 'rgba(62,207,142,.1)';
    banner.style.border     = '1px solid rgba(62,207,142,.3)';
    banner.style.color      = 'var(--positive)';
    banner.textContent      = `📈 ${data.market === 'JP' ? '日経225' : 'Nasdaq'} 強気相場（新規買い有効）`;
  } else {
    banner.style.background = 'rgba(247,95,95,.1)';
    banner.style.border     = '1px solid rgba(247,95,95,.3)';
    banner.style.color      = 'var(--negative)';
    banner.textContent      = `📉 ${data.market === 'JP' ? '日経225' : 'Nasdaq'} 弱気相場（新規買い抑制中）`;
  }

  // 売りシグナル
  const sellCard  = document.getElementById('sell-signal-card');
  const sellTable = document.getElementById('sell-signal-table');
  if (data.sell_signals && data.sell_signals.length > 0) {
    sellCard.classList.remove('hidden');
    sellTable.innerHTML = data.sell_signals.map(s => {
      const pnl  = s.pnl_pct || 0;
      const cls  = pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
      const sign = pnl >= 0 ? '+' : '';
      const cur  = data.market === 'JP' ? '¥' : '$';
      return `<tr>
        <td><strong>${s.ticker}</strong><br><small style="color:var(--text-muted)">${s.label||''}</small></td>
        <td>${cur}${(s.current_price||0).toLocaleString()}</td>
        <td class="${cls}">${sign}${pnl.toFixed(1)}%</td>
        <td>${s.reason||''}</td>
        <td style="font-size:12px;color:var(--text-muted)">${s.explanation||''}</td>
      </tr>`;
    }).join('');
  } else {
    sellCard.classList.add('hidden');
  }

  // 買いシグナル
  const buyCard  = document.getElementById('buy-signal-card');
  const buyTable = document.getElementById('buy-signal-table');
  if (data.buy_signals && data.buy_signals.length > 0) {
    buyCard.classList.remove('hidden');
    buyTable.innerHTML = data.buy_signals.map(s => {
      const cur   = data.market === 'JP' ? '¥' : '$';
      const price = s.details?.price || 0;
      const posSize = 140000;
      const shares  = price > 0 ? (posSize / price).toFixed(1) : '—';
      return `<tr>
        <td><strong>${s.ticker}</strong><br><small style="color:var(--text-muted)">${s.label||''}</small></td>
        <td><span class="badge">${s.score}点</span></td>
        <td>${cur}${price.toLocaleString('ja-JP', {maximumFractionDigits:2})}</td>
        <td>¥140,000<br><small style="color:var(--text-muted)">${shares}株</small></td>
        <td style="font-size:12px;color:var(--text-muted);max-width:240px">${s.explanation||''}</td>
      </tr>`;
    }).join('');
  } else {
    buyCard.classList.add('hidden');
  }

  // スコアランキング
  const sigLabels = { buy: '📈 買い', watch: '👀 監視', none: '—' };
  const sigColors = { buy: 'var(--positive)', watch: 'var(--warning)', none: 'var(--text-muted)' };
  const rankTable = document.getElementById('score-ranking-table');
  rankTable.innerHTML = (data.top_scored || []).map((s, i) => {
    const cur = data.market === 'JP' ? '¥' : '$';
    const sig = s.signal || 'none';
    return `<tr>
      <td>${i + 1}</td>
      <td><strong>${s.ticker}</strong><br><small style="color:var(--text-muted)">${s.label||''}</small></td>
      <td><span class="badge">${s.score}点</span></td>
      <td class="${s.momentum_6m_pct >= 0 ? 'pnl-positive' : 'pnl-negative'}">${s.momentum_6m_pct >= 0 ? '+' : ''}${(s.momentum_6m_pct||0).toFixed(1)}%</td>
      <td>${(s.rsi||0).toFixed(0)}</td>
      <td style="color:${sigColors[sig]}">${sigLabels[sig]}</td>
    </tr>`;
  }).join('');
}

function setSignalStatus(msg, isError = false) {
  const el = document.getElementById('signal-status');
  el.textContent = msg;
  el.style.color = isError ? 'var(--negative)' : 'var(--text-muted)';
}

// ─── シグナル自動ロード ────────────────────────────────

let _signalAutoLoaded = { JP: false, US: false };

async function autoLoadSignal() {
  const market = document.getElementById('signal-market').value;
  // 既にロード済みで6時間以内なら再取得しない
  const cached = _latestSignalTs[market];
  if (cached) {
    const ageHours = (Date.now() - cached) / 3_600_000;
    if (ageHours < 6) {
      await loadSignal();  // キャッシュ表示のみ
      return;
    }
  }
  // 初回またはデータが古い → 自動生成
  await runSignal();
}

const _latestSignalTs = {};

// ─── ポートフォリオ管理 ───────────────────────────────

async function loadPortfolio() {
  try {
    const res  = await fetch('/api/portfolio');
    const data = await res.json();
    renderPortfolio(data);
  } catch (e) {
    console.error('portfolio load error', e);
  }
}

function renderPortfolio(data) {
  const tbody = document.getElementById('portfolio-table');
  if (!data.positions || data.positions.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">保有なし（シグナルに従って手動で追加してください）</td></tr>';
    return;
  }
  tbody.innerHTML = data.positions.map(p => {
    const cur = p.market === 'JP' ? '¥' : '$';
    return `<tr>
      <td><strong>${p.ticker}</strong><br><small style="color:var(--text-muted)">${p.label||''}</small></td>
      <td>${p.market === 'JP' ? '🇯🇵' : '🇺🇸'}</td>
      <td>${p.entry_date||'—'}</td>
      <td>${cur}${(p.entry_price||0).toLocaleString('ja-JP', {maximumFractionDigits:2})}</td>
      <td>${p.shares||0}株</td>
      <td>¥${(p.invested_amount||0).toLocaleString()}</td>
      <td>
        <button class="btn-link" onclick="removePosition('${p.ticker}')" style="color:var(--negative)">削除</button>
      </td>
    </tr>`;
  }).join('');
}

async function addPosition() {
  const ticker = document.getElementById('pos-ticker').value.trim().toUpperCase();
  const price  = parseFloat(document.getElementById('pos-price').value);
  const shares = parseFloat(document.getElementById('pos-shares').value);
  const date   = document.getElementById('pos-date').value;
  const market = document.getElementById('pos-market').value;

  if (!ticker || !price || !shares) {
    alert('ティッカー・取得価格・株数は必須です');
    return;
  }

  try {
    const res = await fetch('/api/portfolio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker, entry_price: price, shares, entry_date: date, market }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    // フォームリセット
    ['pos-ticker','pos-price','pos-shares','pos-date'].forEach(id => {
      document.getElementById(id).value = '';
    });
    await loadPortfolio();
  } catch (e) {
    alert(`追加エラー: ${e.message}`);
  }
}

async function removePosition(ticker) {
  if (!confirm(`${ticker} を保有リストから削除しますか？`)) return;
  try {
    const res = await fetch(`/api/portfolio/${ticker}`, { method: 'DELETE' });
    if (!res.ok) {
      const e = await res.json();
      throw new Error(e.error);
    }
    await loadPortfolio();
  } catch (e) {
    alert(`削除エラー: ${e.message}`);
  }
}
