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
  loadVirtualPortfolioList();
  loadTradingMode();

  // シグナルタブを開いたら自動で最新シグナルを取得・生成
  document.querySelectorAll('.tab').forEach(btn => {
    if (btn.dataset.tab === 'signal') {
      btn.addEventListener('click', async () => {
        await autoLoadSignal();
        await loadVirtualPortfolioList();
      });
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

  if (name === 'dashboard')   loadDashboard();
  if (name === 'trades')      populateSessionSelects();
  if (name === 'analysis')    populateSessionSelects();
  if (name === 'autotrader')  loadAutoTraderData();
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

function onStrategyModeChange(value) {
  const factorCard  = document.getElementById('factor-params-card');
  const paramDetail = document.querySelector('#run-form details.param-details');

  if (value === 'factor') {
    factorCard.classList.remove('hidden');
    if (paramDetail) paramDetail.open = false;
  } else {
    factorCard.classList.add('hidden');
    if (paramDetail) paramDetail.open = true;
  }
}

async function startBacktest() {
  const mode          = document.querySelector('input[name="mode"]:checked').value;
  const start_date    = document.getElementById('start-date').value;
  const end_date      = document.getElementById('end-date').value;
  const iterations    = parseInt(document.getElementById('iterations').value, 10);
  const strategyMode  = document.querySelector('input[name="strategy_mode"]:checked')?.value || 'classic';

  const payload = {
    mode, start_date, end_date, iterations,
    strategy_mode: strategyMode,
    strategy_params: readParams(),
  };

  if (strategyMode === 'factor') {
    payload.factor_params = {
      buy_threshold:      parseFloat(document.getElementById('fp_buy_threshold').value)      || 70,
      stop_loss_pct:      parseFloat(document.getElementById('fp_stop_loss_pct').value)      || 5,
      take_profit_pct:    parseFloat(document.getElementById('fp_take_profit_pct').value)    || 15,
      trailing_stop_pct:  parseFloat(document.getElementById('fp_trailing_stop_pct').value)  || 6,
    };
  }

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

// ─── ダッシュボード（リデザイン版）────────────

let drawdownChart = null;
let _dbAllTrades  = [];

async function loadDashboard() {
  try {
    allSessions = await (await fetch('/api/sessions')).json();
    populateSessionSelects();
    renderSessionsTable(allSessions);

    const sel = document.getElementById('session-select');
    if (allSessions.length > 0 && !sel.value) {
      sel.value = allSessions[0].run_id;
      await loadSessionDetail(allSessions[0].run_id);
    }
  } catch (e) {
    console.warn('ダッシュボード読み込みエラー:', e);
  }
}

async function onSessionChange(runId) {
  if (runId) await loadSessionDetail(runId);
}

async function loadSessionDetail(runId) {
  try {
    const [sessRes, equityRes, tradesRes] = await Promise.all([
      fetch(`/api/sessions/${runId}`),
      fetch(`/api/equity/${runId}`),
      fetch(`/api/trades/${runId}`),
    ]);
    const session    = await sessRes.json();
    const equityData = await equityRes.json();
    const trades     = await tradesRes.json();

    document.getElementById('db-main-grid').style.display = '';
    _updateSessionMeta(session);
    _updateSessionKPIs(session.summary || {});
    _drawEquityChart(equityData, trades);
    _drawDrawdownChart(equityData);
    _dbAllTrades = trades;
    _populateReasonFilter(trades);
    renderDbTradesTable(trades, equityData);
  } catch (e) {
    console.warn('セッション詳細読み込みエラー:', e);
  }
}

function _updateSessionMeta(session) {
  const period = `${(session.start_date||'').slice(0,10)} 〜 ${(session.end_date||'').slice(0,10)}`;
  const cap    = Number(session.initial_capital || 1000000).toLocaleString();
  document.getElementById('db-selector-meta').innerHTML =
    `<span>📅 ${period}</span><span>💰 初期資金 ¥${cap}</span>`;
}

function _updateSessionKPIs(s) {
  const ret   = s.total_return_pct ?? 0;
  const pf    = s.profit_factor    ?? 0;
  const sh    = s.sharpe_ratio     ?? 0;
  const best  = s.best_trade  || {};
  const worst = s.worst_trade || {};

  const kpis = [
    // 主要指標 6枚
    { label: 'リターン',      val: fmt_pct(ret),                             cls: ret >= 0 ? 'positive' : 'negative' },
    { label: '勝率',          val: `${(s.win_rate??0).toFixed(1)}%`,         cls: '' },
    { label: '取引数',        val: `${s.total_trades ?? '—'}件`,             cls: '' },
    { label: 'PF',            val: pf.toFixed(2),                            cls: pf >= 1 ? 'positive' : 'negative' },
    { label: 'シャープ比',    val: sh.toFixed(2),                            cls: sh >= 1 ? 'positive' : sh < 0 ? 'negative' : '' },
    { label: '最大DD',        val: `${(s.max_drawdown_pct??0).toFixed(2)}%`, cls: 'negative' },
    // 補足指標 3枚（アクセント枠）
    { label: '平均保有',      val: `${(s.avg_hold_days??0).toFixed(1)}日`,   cls: '', accent: true },
    { label: `最大益 ${best.ticker||'—'}`,
      val: best.pnl_pct != null ? `+${best.pnl_pct}%` : '—',
      cls: 'positive', sub: best.pnl != null ? `¥${Math.round(best.pnl).toLocaleString()}` : '',
      accent: true },
    { label: `最大損 ${worst.ticker||'—'}`,
      val: worst.pnl_pct != null ? `${worst.pnl_pct}%` : '—',
      cls: 'negative', sub: worst.pnl != null ? `¥${Math.round(worst.pnl).toLocaleString()}` : '',
      accent: true },
  ];

  document.getElementById('db-kpi-row').innerHTML = kpis.map(k =>
    `<div class="db-kpi-card${k.accent ? ' db-kpi-accent' : ''}">
       <div class="db-kpi-label">${k.label}</div>
       <div class="db-kpi-val ${k.cls}">${k.val}</div>
       ${k.sub ? `<div class="db-kpi-sub">${k.sub}</div>` : ''}
     </div>`
  ).join('');
}

function _drawEquityChart(equityData, trades) {
  if (!equityData.length) return;
  const dates    = equityData.map(d => d.date);
  const equities = equityData.map(d => d.total_equity);

  const eqByDate = {};
  equityData.forEach(d => { eqByDate[d.date] = d.total_equity; });
  function nearestEq(dateStr) {
    const key = (dateStr||'').slice(0,10);
    if (eqByDate[key]) return eqByDate[key];
    let best = null, bestDiff = Infinity;
    for (const k of Object.keys(eqByDate)) {
      const diff = Math.abs(new Date(k) - new Date(key));
      if (diff < bestDiff) { bestDiff = diff; best = eqByDate[k]; }
    }
    return best;
  }

  const buyPts = [], sellPts = [];
  trades.forEach(t => {
    const ey = nearestEq(t.entry_date);
    if (ey) buyPts.push({ x: (t.entry_date||'').slice(0,10), y: ey, ticker: t.ticker });
    const xy = nearestEq(t.exit_date);
    if (xy) sellPts.push({ x: (t.exit_date||'').slice(0,10), y: xy, ticker: t.ticker, pnl_pct: t.pnl_pct });
  });

  if (equityChart) equityChart.destroy();
  equityChart = new Chart(
    document.getElementById('equity-chart').getContext('2d'), {
    type: 'line',
    data: { labels: dates, datasets: [
      { type:'line',    label:'資産評価額', data: equities,
        borderColor:'#4f8ef7', backgroundColor:'rgba(79,142,247,0.07)',
        borderWidth:2, pointRadius:0, fill:true, tension:0.3, order:3 },
      { type:'scatter', label:'買い', data: buyPts,
        backgroundColor:'rgba(62,207,142,0.9)', borderColor:'rgba(62,207,142,1)',
        pointStyle:'triangle', pointRadius:7, pointHoverRadius:10, order:1 },
      { type:'scatter', label:'売り', data: sellPts,
        backgroundColor:'rgba(247,95,95,0.9)', borderColor:'rgba(247,95,95,1)',
        pointStyle:'triangle', rotation:180, pointRadius:7, pointHoverRadius:10, order:2 },
    ]},
    options: {
      responsive:true, maintainAspectRatio:false,
      interaction:{ mode:'index', intersect:false },
      plugins:{
        legend:{ labels:{ color:'#7c85a2', usePointStyle:true, boxWidth:10, padding:14 } },
        tooltip:{ callbacks:{ label: ctx => {
          if (ctx.datasetIndex === 0) return ` 資産: ¥${ctx.parsed.y.toLocaleString('ja-JP')}`;
          const r = ctx.raw;
          if (ctx.datasetIndex === 1) return ` ▲ 買い: ${r.ticker}`;
          return ` ▼ 売り: ${r.ticker} (${r.pnl_pct>=0?'+':''}${(r.pnl_pct||0).toFixed(1)}%)`;
        }}},
      },
      scales:{
        x:{ ticks:{ color:'#7c85a2', maxTicksLimit:10 }, grid:{ color:'#2e3350' } },
        y:{ ticks:{ color:'#7c85a2', callback: v=>`¥${(v/10000).toFixed(0)}万` }, grid:{ color:'#2e3350' } },
      },
    },
  });
}

function _drawDrawdownChart(equityData) {
  if (!equityData.length) return;
  const dates = equityData.map(d => d.date);
  let peak = -Infinity;
  const dd = equityData.map(d => {
    if (d.total_equity > peak) peak = d.total_equity;
    return peak > 0 ? (d.total_equity - peak) / peak * 100 : 0;
  });
  if (drawdownChart) drawdownChart.destroy();
  drawdownChart = new Chart(
    document.getElementById('drawdown-chart').getContext('2d'), {
    type:'line',
    data:{ labels:dates, datasets:[{
      data:dd, borderColor:'rgba(247,95,95,0.7)', backgroundColor:'rgba(247,95,95,0.15)',
      borderWidth:1, pointRadius:0, fill:true, tension:0.3,
    }]},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{ display:false },
        tooltip:{ callbacks:{ label: ctx=>` DD: ${ctx.parsed.y.toFixed(2)}%` } }},
      scales:{
        x:{ ticks:{ color:'#7c85a2', maxTicksLimit:10 }, grid:{ color:'#2e3350' } },
        y:{ suggestedMax:0, ticks:{ color:'#7c85a2', callback: v=>`${v.toFixed(0)}%` }, grid:{ color:'#2e3350' } },
      },
    },
  });
}

function _populateReasonFilter(trades) {
  const reasons = [...new Set(trades.map(t => t.exit_reason).filter(Boolean))].sort();
  const sel = document.getElementById('db-reason-filter');
  sel.innerHTML = '<option value="">すべての理由</option>' +
    reasons.map(r => `<option value="${escHtml(r)}">${escHtml(r)}</option>`).join('');
}

function filterDbTrades() {
  const reason = document.getElementById('db-reason-filter').value;
  renderDbTradesTable(
    reason ? _dbAllTrades.filter(t => t.exit_reason === reason) : _dbAllTrades,
    _dbEquityData
  );
}

let _dbEquityData = [];

function _fmtDate(dateStr) {
  if (!dateStr) return '—';
  return dateStr.slice(5, 10).replace('-', '/');  // MM/DD
}

function _fmtPnl(pnl) {
  const abs = Math.abs(Math.round(pnl || 0));
  if (abs >= 10000) return `${(pnl >= 0 ? '+' : '-')}${(abs / 10000).toFixed(1)}万`;
  return `${pnl >= 0 ? '+' : '-'}¥${abs.toLocaleString()}`;
}

function renderDbTradesTable(trades, equityData) {
  if (equityData) _dbEquityData = equityData;

  // 日付 → 総資産のルックアップ
  const eqMap = {};
  (_dbEquityData || []).forEach(d => { eqMap[d.date] = d.total_equity; });
  function nearestEq(dateStr) {
    const key = (dateStr || '').slice(0, 10);
    if (eqMap[key] != null) return eqMap[key];
    let best = null, bestDiff = Infinity;
    for (const k of Object.keys(eqMap)) {
      const diff = Math.abs(new Date(k) - new Date(key));
      if (diff < bestDiff) { bestDiff = diff; best = eqMap[k]; }
    }
    return best;
  }

  const tbody = document.getElementById('db-trades-tbody');
  document.getElementById('db-trade-count').textContent = `(${trades.length}件)`;
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">取引なし</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map((t, i) => {
    const pct    = t.pnl_pct ?? 0;
    const win    = pct > 0;
    const pnlCls = win ? 'pnl-positive' : 'pnl-negative';
    const rowCls = win ? 'db-trade-row-win' : 'db-trade-row-loss';
    const eq     = nearestEq(t.exit_date);
    const eqStr  = eq != null ? `¥${(eq / 10000).toFixed(0)}万` : '—';
    return `<tr class="${rowCls}" onclick="onDbTradeClick(this,${i})">
      <td class="td-ticker">${escHtml(t.ticker)}</td>
      <td class="col-center">${_fmtDate(t.entry_date)}</td>
      <td class="col-center">${_fmtDate(t.exit_date)}</td>
      <td class="col-right" style="color:var(--text-muted)">${t.hold_days??'—'}日</td>
      <td class="col-right ${pnlCls}">${pct>=0?'+':''}${pct.toFixed(1)}%</td>
      <td class="col-right ${pnlCls}">${_fmtPnl(t.pnl)}</td>
      <td class="col-right" style="color:var(--text-muted);font-size:12px">${eqStr}</td>
      <td class="td-reason" title="${escHtml(t.exit_reason||'')}">${escHtml(t.exit_reason||'—')}</td>
    </tr>`;
  }).join('');
}

function onDbTradeClick(row, idx) {
  document.querySelectorAll('#db-trades-tbody tr').forEach(r => r.classList.remove('db-trade-row-selected'));
  row.classList.add('db-trade-row-selected');
  const trade = _dbAllTrades[idx];
  if (!trade || !equityChart) return;
  const labels = equityChart.data.labels;
  const pos = labels.indexOf((trade.entry_date||'').slice(0,10));
  if (pos >= 0) {
    equityChart.tooltip.setActiveElements([{ datasetIndex:0, index:pos }], { x:0, y:0 });
    equityChart.update();
  }
}

function renderSessionsTable(sessions) {
  const tbody = document.getElementById('sessions-tbody');
  if (!sessions.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">データなし</td></tr>';
    return;
  }
  tbody.innerHTML = sessions.map(s => {
    const ret = s.total_return_pct ?? 0;
    const cls = ret >= 0 ? 'pnl-positive' : 'pnl-negative';
    return `<tr>
      <td>${(s.start_date||'').slice(0,10)} 〜 ${(s.end_date||'').slice(0,10)}</td>
      <td class="${cls}">${fmt_pct(ret)}</td>
      <td>${(s.win_rate??0).toFixed(1)}%</td>
      <td>${(s.sharpe_ratio??0).toFixed(2)}</td>
      <td class="pnl-negative">${(s.max_drawdown_pct??0).toFixed(2)}%</td>
      <td>${(s.profit_factor??0).toFixed(2)}</td>
      <td>${s.total_trades??'—'}</td>
      <td><button class="btn-link" onclick="viewSession('${s.run_id}')">表示</button></td>
    </tr>`;
  }).join('');
}

function viewSession(runId) {
  document.getElementById('session-select').value = runId;
  loadSessionDetail(runId);
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
    if (!sel) return;
    const cur  = sel.value;
    const opts = allSessions.map(s => {
      const period = `${(s.start_date||'').slice(0,10)} 〜 ${(s.end_date||'').slice(0,10)}`;
      const ret    = s.total_return_pct != null ? ` (${s.total_return_pct >= 0 ? '+' : ''}${s.total_return_pct.toFixed(1)}%)` : '';
      return `<option value="${s.run_id}" ${s.run_id === cur ? 'selected' : ''}>${period}${ret}</option>`;
    }).join('');
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
      const alreadyVirtual = _virtualTickers.has(s.ticker);
      return `<tr>
        <td><strong>${s.ticker}</strong><br><small style="color:var(--text-muted)">${s.label||''}</small></td>
        <td><span class="badge">${s.score}点</span></td>
        <td>${cur}${price.toLocaleString('ja-JP', {maximumFractionDigits:2})}</td>
        <td>¥140,000<br><small style="color:var(--text-muted)">${shares}株</small></td>
        <td style="font-size:12px;color:var(--text-muted);max-width:240px">${s.explanation||''}</td>
        <td>
          ${alreadyVirtual
            ? `<span style="font-size:11px;color:var(--positive)">✓ 仮登録済</span>`
            : `<button class="btn-secondary" style="font-size:11px;padding:3px 10px;white-space:nowrap"
                data-ticker="${escHtml(s.ticker)}"
                data-price="${price}"
                data-label="${escHtml(s.label||'')}"
                data-market="${escHtml(data.market)}"
                data-score="${s.score}"
                onclick="virtualRegisterClick(this)">仮登録</button>`
          }
        </td>
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

// =============================================
//  仮保有ポートフォリオ
// =============================================

// 仮保有中のティッカーセット（ボタン表示制御用）
let _virtualTickers = new Set();

/**
 * ページ初期化・仮登録直後に呼ぶ。
 * 価格取得なしで高速にテーブルを表示する。
 */
async function loadVirtualPortfolioList() {
  try {
    const res  = await fetch('/api/virtual-portfolio/list');
    const data = await res.json();
    const positions = data.positions || [];
    _virtualTickers = new Set(positions.map(p => p.ticker));
    renderVirtualPortfolioFromList(positions);
  } catch (e) {
    console.warn('仮保有リスト取得失敗:', e);
  }
}

/** 価格なしで一覧を描画する（登録直後の即時表示用） */
function renderVirtualPortfolioFromList(positions) {
  const tbody   = document.getElementById('virtual-portfolio-table');
  const summary = document.getElementById('virtual-portfolio-summary');

  if (!positions.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty">仮保有なし（買いシグナルで「仮登録」してください）</td></tr>';
    summary.classList.add('hidden');
    return;
  }

  tbody.innerHTML = positions.map(p => {
    const cur      = p.market === 'JP' ? '¥' : '$';
    const holdDays = Math.max(0, Math.floor(
      (Date.now() - new Date(p.entry_date || Date.now()).getTime()) / 86400000
    ));
    return `<tr>
      <td><strong>${escHtml(p.ticker)}</strong><br>
          <small style="color:var(--text-muted)">${escHtml(p.label||'')}</small></td>
      <td>${p.signal_score > 0 ? `<span class="badge">${p.signal_score}点</span>` : '—'}</td>
      <td>${p.entry_date || '—'}</td>
      <td>${cur}${(p.entry_price||0).toLocaleString('ja-JP', {maximumFractionDigits:2})}</td>
      <td style="color:var(--text-muted);font-size:11px">「現在値を更新」で取得</td>
      <td>—</td>
      <td>—</td>
      <td>${holdDays}日</td>
      <td><button class="btn-link" onclick="removeVirtualPosition('${escHtml(p.ticker)}')"
              style="color:var(--negative)">削除</button></td>
    </tr>`;
  }).join('');

  summary.classList.add('hidden');
}

/** 現在値を取得して損益付きで表示する（「現在値を更新」ボタン押下時） */
async function loadVirtualPortfolio() {
  const tbody = document.getElementById('virtual-portfolio-table');
  tbody.innerHTML = '<tr><td colspan="9" class="empty" style="color:var(--text-muted)">現在値を取得中... しばらくお待ちください</td></tr>';

  try {
    const res  = await fetch('/api/virtual-portfolio');
    // サーバーがHTMLを返した場合も安全に処理する
    const text = await res.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">サーバーエラー。サーバーログを確認してください。</td></tr>';
      return;
    }
    if (!res.ok) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty">エラー: ${escHtml(data.error || '不明なエラー')}</td></tr>`;
      return;
    }
    _virtualTickers = new Set((data.positions || []).map(p => p.ticker));
    renderVirtualPortfolio(data);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty">読み込みエラー: ${escHtml(e.message)}</td></tr>`;
  }
}

function renderVirtualPortfolio(data) {
  const tbody   = document.getElementById('virtual-portfolio-table');
  const summary = document.getElementById('virtual-portfolio-summary');
  const positions = data.positions || [];

  if (!positions.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty">仮保有なし（買いシグナルで「仮登録」してください）</td></tr>';
    summary.classList.add('hidden');
    return;
  }

  tbody.innerHTML = positions.map(p => {
    const cur      = p.market === 'JP' ? '¥' : '$';
    const pnlPos   = p.pnl_pct >= 0;
    const pnlCls   = pnlPos ? 'pnl-positive' : 'pnl-negative';
    const sign     = pnlPos ? '+' : '';
    const curPrice = p.current_price != null
      ? `${cur}${p.current_price.toLocaleString('ja-JP', {maximumFractionDigits: 2})}`
      : `<span style="color:var(--negative);font-size:11px">${p.fetch_error || '—'}</span>`;

    return `<tr>
      <td><strong>${escHtml(p.ticker)}</strong><br>
          <small style="color:var(--text-muted)">${escHtml(p.label||'')}</small></td>
      <td>${p.signal_score > 0 ? `<span class="badge">${p.signal_score}点</span>` : '—'}</td>
      <td>${p.entry_date || '—'}</td>
      <td>${cur}${(p.entry_price||0).toLocaleString('ja-JP', {maximumFractionDigits:2})}</td>
      <td>${curPrice}</td>
      <td class="${pnlCls}">${sign}${(p.pnl_pct||0).toFixed(2)}%</td>
      <td class="${pnlCls}">${sign}¥${Math.abs(p.pnl_amount||0).toLocaleString()}</td>
      <td>${p.hold_days}日</td>
      <td><button class="btn-link" onclick="removeVirtualPosition('${escHtml(p.ticker)}')"
              style="color:var(--negative)">削除</button></td>
    </tr>`;
  }).join('');

  // 合計サマリー
  summary.classList.remove('hidden');
  document.getElementById('vp-total-invested').textContent =
    `¥${(data.total_invested||0).toLocaleString()}`;
  document.getElementById('vp-total-current').textContent =
    `¥${(data.total_current||0).toLocaleString()}`;

  const pnl    = data.total_pnl || 0;
  const pnlPct = data.total_pnl_pct || 0;
  const sign   = pnl >= 0 ? '+' : '';
  const cls    = pnl >= 0 ? 'positive' : 'negative';
  document.getElementById('vp-total-pnl').innerHTML =
    `<span class="${cls}">${sign}¥${Math.abs(pnl).toLocaleString()}</span>`;
  document.getElementById('vp-total-pnl-pct').innerHTML =
    `<span class="${cls}">(${sign}${pnlPct.toFixed(2)}%)</span>`;
}

/** 買いシグナル行の「仮登録」ボタンから呼ばれる */
function virtualRegisterClick(btn) {
  const ticker = btn.dataset.ticker;
  const price  = parseFloat(btn.dataset.price);
  const label  = btn.dataset.label;
  const market = btn.dataset.market;
  const score  = parseInt(btn.dataset.score, 10);
  addVirtualPosition(ticker, price, label, market, score);
}

async function addVirtualPosition(ticker, price, label, market, score) {
  if (price <= 0) {
    alert('価格が取得できていません。シグナルを再生成してください。');
    return;
  }
  const today = new Date().toISOString().slice(0, 10);
  try {
    const res = await fetch('/api/virtual-portfolio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ticker,
        entry_price:  price,
        market,
        label,
        signal_score: score,
        entry_date:   today,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    _virtualTickers.add(ticker);
    // ボタンを「登録済」に変更
    const btn = document.querySelector(`[data-ticker="${ticker}"]`);
    if (btn) {
      btn.outerHTML = `<span style="font-size:11px;color:var(--positive)">✓ 仮登録済</span>`;
    }
    // 仮保有テーブルを即時表示（価格取得なし）
    await loadVirtualPortfolioList();
    // シグナルタブが見えていれば仮保有カードへスクロール
    const vpSection = document.getElementById('virtual-portfolio-table');
    if (vpSection) vpSection.closest('.card')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (e) {
    alert(`仮登録エラー: ${e.message}`);
  }
}

async function removeVirtualPosition(ticker) {
  if (!confirm(`${ticker} を仮保有リストから削除しますか？`)) return;
  try {
    const res = await fetch(`/api/virtual-portfolio/${ticker}`, { method: 'DELETE' });
    if (!res.ok) {
      const e = await res.json();
      throw new Error(e.error);
    }
    _virtualTickers.delete(ticker);
    await loadVirtualPortfolio();
  } catch (e) {
    alert(`削除エラー: ${e.message}`);
  }
}

// =============================================
//  自動トレード タブ
// =============================================

async function loadAutoTraderData() {
  await Promise.all([
    loadRealTradesSummary(),
    loadRealPositions(),
    loadRealTrades(),
    loadTradeDiary(),
  ]);
}

async function loadRealTradesSummary() {
  try {
    const res  = await fetch('/api/real-trades/summary');
    const data = await res.json();
    if (data.error) return;

    const pnl    = data.total_pnl || 0;
    const pnlPct = data.avg_pnl_pct || 0;
    const pnlCls = pnl >= 0 ? 'positive' : 'negative';
    const sign   = pnl >= 0 ? '+' : '';

    document.getElementById('at-kpi-total').textContent    = `${data.total_trades}件`;
    document.getElementById('at-kpi-winrate').textContent  = `${data.win_rate}%`;
    document.getElementById('at-kpi-pnl').innerHTML        =
      `<span class="${pnlCls}">${sign}¥${Math.abs(pnl).toLocaleString()}</span>`;
    document.getElementById('at-kpi-avg-pnl').innerHTML    =
      `<span class="${pnlPct >= 0 ? 'positive' : 'negative'}">${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%</span>`;
    document.getElementById('at-kpi-pf').textContent       = data.profit_factor.toFixed(2);
    document.getElementById('at-kpi-hold').textContent     = `${data.avg_hold_days}日`;

    if (data.total_trades > 0) {
      const stats = document.getElementById('at-trade-stats');
      stats.style.display = 'flex';
      document.getElementById('at-wins').textContent   = `${data.wins}件`;
      document.getElementById('at-losses').textContent = `${data.losses}件`;
      document.getElementById('at-best').textContent   = `+${data.best_trade_pct}%`;
      document.getElementById('at-worst').textContent  = `${data.worst_trade_pct}%`;
    }
  } catch (e) {
    console.warn('実取引サマリー取得失敗:', e);
  }
}

async function loadRealPositions() {
  try {
    const res  = await fetch('/api/portfolio');
    const data = await res.json();
    const tbody = document.getElementById('at-positions-table');

    if (!data.positions || data.positions.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">保有なし（自動取引稼働後に表示されます）</td></tr>';
      return;
    }

    tbody.innerHTML = data.positions.map(p => {
      const cur = p.market === 'JP' ? '¥' : '$';
      return `<tr>
        <td><strong>${escHtml(p.ticker)}</strong><br>
            <small style="color:var(--text-muted)">${escHtml(p.label||'')}</small></td>
        <td>${p.market === 'JP' ? '🇯🇵' : '🇺🇸'}</td>
        <td>${p.entry_date || '—'}</td>
        <td>${cur}${(p.entry_price||0).toLocaleString('ja-JP', {maximumFractionDigits:2})}</td>
        <td>${p.shares||0}株</td>
        <td>¥${(p.invested_amount||0).toLocaleString()}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    console.warn('実ポジション取得失敗:', e);
  }
}

async function loadRealTrades() {
  try {
    const res    = await fetch('/api/real-trades');
    const trades = await res.json();
    const tbody  = document.getElementById('at-trades-table');

    if (!Array.isArray(trades) || trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty">実取引なし（自動取引稼働後に表示されます）</td></tr>';
      return;
    }

    tbody.innerHTML = trades.map(t => {
      const pnlPct = t.pnl_pct || 0;
      const pnl    = t.pnl || 0;
      const cls    = pnlPct >= 0 ? 'pnl-positive' : 'pnl-negative';
      const sign   = pnlPct >= 0 ? '+' : '';
      const diaryId = `${t.ticker}_${(t.exit_date||'').replace(/-/g,'')||''}`;
      return `<tr>
        <td><strong>${escHtml(t.ticker)}</strong><br>
            <small style="color:var(--text-muted)">${escHtml(t.label||'')}</small></td>
        <td>${t.entry_date||'—'}</td>
        <td>${t.exit_date||t.logged_at?.slice(0,10)||'—'}</td>
        <td>¥${(t.entry_price||0).toLocaleString()}</td>
        <td>¥${(t.exit_price||0).toLocaleString()}</td>
        <td>${t.shares||0}株</td>
        <td class="${cls}">${sign}¥${Math.abs(pnl).toLocaleString()}</td>
        <td class="${cls}">${sign}${pnlPct.toFixed(2)}%</td>
        <td style="font-size:11px;color:var(--text-muted)">${escHtml(t.exit_reason||'')}</td>
        <td>
          <button class="btn-link" style="font-size:11px"
            onclick="scrollToDiaryEntry('${escHtml(diaryId)}')">日記▼</button>
        </td>
      </tr>`;
    }).join('');
  } catch (e) {
    console.warn('実取引履歴取得失敗:', e);
  }
}

async function loadTradeDiary() {
  try {
    const res     = await fetch('/api/trade-diary');
    const entries = await res.json();
    const container = document.getElementById('at-diary-list');

    if (!Array.isArray(entries) || entries.length === 0) {
      container.innerHTML =
        '<div class="empty" style="padding:20px;text-align:center">日記なし（実取引完了後に自動生成されます）</div>';
      return;
    }

    container.innerHTML = entries.map(e => {
      const a      = e.analysis || {};
      const pnlPct = e.pnl_pct || 0;
      const cls    = pnlPct >= 0 ? 'positive' : 'negative';
      const sign   = pnlPct >= 0 ? '+' : '';
      const icon   = pnlPct >= 0 ? '✅' : '❌';

      return `<div class="diary-entry" id="diary-${escHtml(e.trade_id||'')}">
        <div class="diary-header">
          <span class="diary-ticker">${icon} <strong>${escHtml(e.ticker)}</strong>
            <span style="color:var(--text-muted);font-size:12px">${escHtml(e.label||'')}</span>
          </span>
          <span class="diary-meta">
            ${e.entry_date||'?'} → ${e.exit_date||'?'}
            &nbsp;|&nbsp;
            <span class="${cls}">${sign}${pnlPct.toFixed(2)}%</span>
            &nbsp;/&nbsp;
            <span class="${cls}">${sign}¥${Math.abs(e.pnl||0).toLocaleString()}</span>
          </span>
        </div>
        ${a.summary ? `<div class="diary-summary">${escHtml(a.summary)}</div>` : ''}
        <div class="diary-body">
          ${a.win_loss_reason ? `<div class="diary-section"><span class="diary-label">📊 原因</span><span>${escHtml(a.win_loss_reason)}</span></div>` : ''}
          ${a.what_went_well  ? `<div class="diary-section"><span class="diary-label">✓ 良かった点</span><span>${escHtml(a.what_went_well)}</span></div>` : ''}
          ${a.what_went_wrong ? `<div class="diary-section"><span class="diary-label">✗ 反省点</span><span style="color:var(--negative)">${escHtml(a.what_went_wrong)}</span></div>` : ''}
          ${a.lesson          ? `<div class="diary-section"><span class="diary-label">💡 教訓</span><strong>${escHtml(a.lesson)}</strong></div>` : ''}
          ${a.next_action     ? `<div class="diary-section"><span class="diary-label">→ 次のアクション</span><span>${escHtml(a.next_action)}</span></div>` : ''}
        </div>
        <div class="diary-footer">
          売却理由: ${escHtml(e.exit_reason||'—')} &nbsp;|&nbsp; 記録: ${(e.created_at||'').slice(0,16).replace('T',' ')}
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    console.warn('日記取得失敗:', e);
  }
}

function scrollToDiaryEntry(tradeId) {
  const el = document.getElementById(`diary-${tradeId}`);
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.style.outline = '2px solid var(--positive)';
    setTimeout(() => { el.style.outline = ''; }, 2000);
  } else {
    document.getElementById('at-diary-card')?.scrollIntoView({ behavior: 'smooth' });
  }
}

// ─── 累積損益チャート ─────────────────────────────────────────

let _atEquityChart = null;

async function loadAtEquityChart() {
  try {
    const res  = await fetch('/api/real-trades/equity');
    const data = await res.json();
    if (!data.labels || data.labels.length === 0) return;

    const card = document.getElementById('at-equity-card');
    card.style.display = '';

    const ctx = document.getElementById('at-equity-chart').getContext('2d');
    if (_atEquityChart) _atEquityChart.destroy();

    const pnls = data.cumulative_pnl || [];
    const colors = pnls.map(v => v >= 0 ? 'rgba(62,207,142,0.8)' : 'rgba(247,95,95,0.8)');

    _atEquityChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: data.labels,
        datasets: [{
          label: '累積損益（円）',
          data: pnls,
          borderColor: 'rgba(62,207,142,1)',
          backgroundColor: 'rgba(62,207,142,0.1)',
          borderWidth: 2,
          pointBackgroundColor: colors,
          pointRadius: 5,
          fill: true,
          tension: 0.3,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => {
                const v = ctx.parsed.y;
                return ` ${v >= 0 ? '+' : ''}¥${Math.abs(v).toLocaleString()}`;
              },
            },
          },
        },
        scales: {
          x: { ticks: { color: '#8b9cb3', font: { size: 11 } }, grid: { color: 'rgba(255,255,255,.05)' } },
          y: {
            ticks: {
              color: '#8b9cb3',
              callback: v => `¥${(v/10000).toFixed(0)}万`,
            },
            grid: { color: 'rgba(255,255,255,.05)' },
          },
        },
      },
    });
  } catch (e) {
    console.warn('累積損益チャート取得失敗:', e);
  }
}

// ─── 一時停止 / 再開 ──────────────────────────────────────────

async function loadTraderStatus() {
  try {
    const res  = await fetch('/api/trader/status');
    const data = await res.json();
    _applyTraderStatus(data.paused);
  } catch (e) {
    console.warn('トレーダー状態取得失敗:', e);
  }
}

function _applyTraderStatus(paused) {
  const dot    = document.getElementById('at-status-dot');
  const label  = document.getElementById('at-status-label');
  const pause  = document.getElementById('at-pause-btn');
  const resume = document.getElementById('at-resume-btn');

  if (paused) {
    dot.style.background   = 'var(--warning, #f5a623)';
    label.textContent      = '⏸ 一時停止中';
    label.style.color      = 'var(--warning, #f5a623)';
    pause.style.display    = 'none';
    resume.style.display   = '';
  } else {
    dot.style.background   = 'var(--positive)';
    label.textContent      = '▶ 稼働中';
    label.style.color      = 'var(--positive)';
    pause.style.display    = '';
    resume.style.display   = 'none';
  }
}

async function pauseTrader() {
  if (!confirm('自動取引を一時停止しますか？\n（進行中の監視は停止しますが、改善分析は続きます）')) return;
  try {
    const res  = await fetch('/api/trader/pause', { method: 'POST' });
    const data = await res.json();
    _applyTraderStatus(true);
    alert('一時停止しました。再開するには「再開」ボタンを押してください。');
  } catch (e) {
    alert(`エラー: ${e.message}`);
  }
}

async function resumeTrader() {
  try {
    const res  = await fetch('/api/trader/resume', { method: 'POST' });
    const data = await res.json();
    _applyTraderStatus(false);
    alert('自動取引を再開しました。次の取引時間から有効です。');
  } catch (e) {
    alert(`エラー: ${e.message}`);
  }
}

// ─────────────────────────────────────────────
// ペーパートレード
// ─────────────────────────────────────────────

async function loadPaperTraderStatus() {
  try {
    const res = await fetch("/api/paper-trader/status");
    const d = await res.json();
    if (d.error) { document.getElementById("pt-msg").textContent = d.error; return; }
    _renderPaperStatus(d);
  } catch (e) { console.error("ペーパー状態取得失敗", e); }
}

function _renderPaperStatus(d) {
  const fmt = v => (v >= 0 ? "+" : "") + v.toLocaleString("ja-JP", {maximumFractionDigits: 2});
  const fmtY = v => "¥" + Math.abs(v).toLocaleString("ja-JP");

  document.getElementById("pt-equity").textContent   = fmtY(d.total_equity);
  document.getElementById("pt-cash").textContent     = fmtY(d.cash);
  document.getElementById("pt-trades").textContent   = d.trade_count + "件";
  document.getElementById("pt-winrate").textContent  = d.win_rate + "%";

  const ret = d.total_return_pct;
  const retEl = document.getElementById("pt-return");
  retEl.textContent = (ret >= 0 ? "+" : "") + ret.toFixed(2) + "%";
  retEl.style.color = ret >= 0 ? "var(--positive)" : "var(--negative)";

  // 保有ポジション
  const posBody = document.getElementById("pt-positions");
  if (!d.positions || d.positions.length === 0) {
    posBody.innerHTML = '<tr><td colspan="6" class="empty">保有なし</td></tr>';
  } else {
    posBody.innerHTML = d.positions.map(p => {
      const pnlColor = p.pnl >= 0 ? "var(--positive)" : "var(--negative)";
      return `<tr>
        <td>${p.label || p.ticker}</td>
        <td>${(p.entry_date || "").slice(0,10)}</td>
        <td>¥${p.entry_price.toLocaleString("ja-JP")}</td>
        <td>${p.current_price ? "¥" + p.current_price.toLocaleString("ja-JP") : "—"}</td>
        <td style="color:${pnlColor}">${p.pnl >= 0 ? "+" : ""}¥${Math.abs(p.pnl).toLocaleString("ja-JP")}</td>
        <td style="color:${pnlColor}">${p.pnl_pct >= 0 ? "+" : ""}${p.pnl_pct.toFixed(2)}%</td>
      </tr>`;
    }).join("");
  }

  // 直近取引
  const histBody = document.getElementById("pt-history");
  if (!d.recent_trades || d.recent_trades.length === 0) {
    histBody.innerHTML = '<tr><td colspan="5" class="empty">取引なし</td></tr>';
  } else {
    histBody.innerHTML = d.recent_trades.map(t => {
      const c = t.pnl >= 0 ? "var(--positive)" : "var(--negative)";
      return `<tr>
        <td>${t.label || t.ticker}</td>
        <td>${(t.exit_date || "").slice(0,10)}</td>
        <td style="color:${c}">${t.pnl >= 0 ? "+" : ""}¥${Math.abs(t.pnl).toLocaleString("ja-JP")}</td>
        <td style="color:${c}">${t.pnl_pct >= 0 ? "+" : ""}${t.pnl_pct.toFixed(2)}%</td>
        <td style="font-size:11px;color:var(--text-muted)">${t.reason || ""}</td>
      </tr>`;
    }).join("");
  }
}

async function runPaperCycle() {
  const msgEl = document.getElementById("pt-msg");
  msgEl.textContent = "シグナル取得・実行中...（数分かかる場合があります）";
  try {
    const res = await fetch("/api/paper-trader/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ market: "JP" }),
    });
    const d = await res.json();
    if (d.error) { msgEl.textContent = "エラー: " + d.error; return; }
    const bought  = (d.bought  || []).map(b => `買い: ${b.ticker}`).join(", ");
    const stopped = (d.stopped || []).map(s => `売り: ${s.ticker}`).join(", ");
    msgEl.textContent = `完了 — ${bought || "買いなし"} / ${stopped || "売りなし"}  (市場: ${d.market_bullish ? "強気" : "弱気"})`;
    if (d.portfolio) _renderPaperStatus(d.portfolio);
  } catch (e) {
    msgEl.textContent = "通信エラー: " + e.message;
  }
}

async function resetPaperTrader() {
  if (!confirm("ペーパーポートフォリオをリセットしますか？\n取引履歴・ポジションがすべて消去されます。")) return;
  try {
    await fetch("/api/paper-trader/reset", { method: "POST" });
    await loadPaperTraderStatus();
    document.getElementById("pt-msg").textContent = "リセット完了";
  } catch (e) { alert("リセット失敗: " + e.message); }
}

// ─────────────────────────────────────────────
// 設定モーダル
// ─────────────────────────────────────────────

async function openSettings() {
  document.getElementById("settings-overlay").classList.add("open");
  document.getElementById("settings-save-msg").textContent = "";
  document.getElementById("kabu-test-result").textContent = "";
  await _loadSettingsIntoForm();
}

function closeSettings() {
  document.getElementById("settings-overlay").classList.remove("open");
}

function closeSettingsIfOutside(e) {
  if (e.target === document.getElementById("settings-overlay")) closeSettings();
}

// 設定タブ切り替え
document.querySelectorAll(".stab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".stab").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".stab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("stab-" + btn.dataset.stab).classList.add("active");
  });
});

async function _loadSettingsIntoForm() {
  try {
    const res = await fetch("/api/settings");
    const s = await res.json();
    const has = s._has_value || {};

    document.getElementById("s-kabu-url").value       = s.kabu_api_base_url || "";
    document.getElementById("s-kabu-exchange").value  = s.kabu_exchange_code || 1;
    document.getElementById("s-kabu-pw").value        = has.kabu_api_password   ? "●●●●●●●●" : "";
    document.getElementById("s-kabu-trade-pw").value  = has.kabu_trade_password ? "●●●●●●●●" : "";
    document.getElementById("s-line-token").value     = has.line_channel_token  ? "●●●●●●●●" : "";
    document.getElementById("s-line-uid").value       = s.line_user_id || "";
    document.getElementById("s-anthropic-key").value  = has.anthropic_api_key   ? "●●●●●●●●" : "";
    document.getElementById("s-trading-mode").value   = s.trading_mode || "paper";
    document.getElementById("s-paper-capital").value  = s.paper_initial_capital || 1000000;
    _updateModeNote(s.trading_mode || "paper");
    document.getElementById("s-trading-mode").onchange = e => _updateModeNote(e.target.value);
  } catch (e) {
    console.error("設定の読み込みエラー:", e);
  }
}

function _updateModeNote(mode) {
  document.getElementById("mode-note-paper").style.display = mode === "paper" ? "" : "none";
  document.getElementById("mode-note-live").style.display  = mode === "live"  ? "" : "none";
}

async function saveSettings() {
  const payload = {
    kabu_api_base_url:    document.getElementById("s-kabu-url").value.trim(),
    kabu_exchange_code:   parseInt(document.getElementById("s-kabu-exchange").value) || 1,
    kabu_api_password:    document.getElementById("s-kabu-pw").value,
    kabu_trade_password:  document.getElementById("s-kabu-trade-pw").value,
    line_channel_token:   document.getElementById("s-line-token").value,
    line_user_id:         document.getElementById("s-line-uid").value.trim(),
    anthropic_api_key:    document.getElementById("s-anthropic-key").value,
    trading_mode:         document.getElementById("s-trading-mode").value,
    paper_initial_capital:parseInt(document.getElementById("s-paper-capital").value) || 1000000,
  };
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const msg = document.getElementById("settings-save-msg");
    msg.textContent = "✓ 保存しました";
    loadTradingMode();
    setTimeout(() => { msg.textContent = ""; closeSettings(); }, 1200);
  } catch (e) {
    document.getElementById("settings-save-msg").textContent = "保存に失敗しました";
  }
}

async function testLineConnection() {
  const el = document.getElementById("line-test-result");
  el.textContent = "送信中...";
  el.className = "test-result";
  await saveSettings();  // 入力中の値を先に保存してから送信
  try {
    const res = await fetch("/api/settings/test_line", { method: "POST" });
    const data = await res.json();
    el.textContent = data.message;
    el.className = "test-result " + (data.status === "ok" ? "ok" : "err");
  } catch (e) {
    el.textContent = "通信エラー";
    el.className = "test-result err";
  }
}

async function testKabuConnection() {
  const el = document.getElementById("kabu-test-result");
  el.textContent = "接続中...";
  el.className = "test-result";
  try {
    const res = await fetch("/api/settings/test_kabu", { method: "POST" });
    const data = await res.json();
    el.textContent = data.message;
    el.className = "test-result " + (data.status === "ok" ? "ok" : "err");
  } catch (e) {
    el.textContent = "通信エラー";
    el.className = "test-result err";
  }
}

function togglePwd(id, btn) {
  const inp = document.getElementById(id);
  if (inp.type === "password") {
    inp.type = "text";
    btn.textContent = "🙈";
  } else {
    inp.type = "password";
    btn.textContent = "👁";
  }
}

// ─────────────────────────────────────────────
// 取引モード表示
// ─────────────────────────────────────────────

async function loadTradingMode() {
  try {
    const res = await fetch('/api/trading-mode');
    const d = await res.json();
    _applyTradingMode(d.trading_mode || 'paper');
  } catch (e) {
    console.warn('取引モード取得失敗:', e);
  }
}

function _applyTradingMode(mode) {
  const isPaper = mode !== 'live';

  // ヘッダーバッジ
  const hBadge = document.getElementById('header-mode-badge');
  if (hBadge) {
    hBadge.textContent = isPaper ? '📄 ペーパー' : '💴 ライブ';
    hBadge.className   = isPaper ? 'mode-badge mode-paper' : 'mode-badge mode-live';
  }

  // タブ内バッジ
  const tBadge = document.getElementById('tab-mode-badge');
  if (tBadge) {
    tBadge.textContent = isPaper ? '📄' : '💴';
    tBadge.className   = isPaper ? 'tab-mode-badge tab-mode-paper' : 'tab-mode-badge tab-mode-live';
  }

  // 自動トレードタブ内バナー
  const banner = document.getElementById('at-mode-banner');
  if (banner) {
    banner.className = isPaper ? 'at-mode-banner at-mode-paper' : 'at-mode-banner at-mode-live';
    document.getElementById('at-mode-icon').textContent  = isPaper ? '📄' : '💴';
    document.getElementById('at-mode-title').textContent = isPaper ? 'ペーパートレードモード' : 'ライブトレードモード';
    document.getElementById('at-mode-desc').textContent  = isPaper
      ? '仮想資金で自動売買をシミュレート中。実際の注文は一切行いません。'
      : '実資金で自動売買中。kabuステーション® との接続が必要です。';
  }

  // ペーパーパネルはペーパーモード時のみ表示
  const paperPanel = document.getElementById('paper-panel');
  if (paperPanel) paperPanel.style.display = isPaper ? '' : 'none';

  // ライブ専用セクションのヘッダーをモードに合わせて変更
  const livePositionsHeader = document.querySelector('#at-positions-table')?.closest('.card')?.querySelector('.card-header');
  if (livePositionsHeader) {
    livePositionsHeader.childNodes[0].textContent = isPaper ? '💼 現在の保有ポジション（ライブモード専用）' : '💼 現在の保有ポジション（実取引）';
  }
}

// loadAutoTraderData に追加ロードを組み込む（上書き）
const _origLoadAutoTraderData = loadAutoTraderData;
loadAutoTraderData = async function () {
  await Promise.all([
    _origLoadAutoTraderData(),
    loadTraderStatus(),
    loadAtEquityChart(),
    loadPaperTraderStatus(),
    loadTradingMode(),
  ]);
};
