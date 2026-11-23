const appState = {
  activePage: "overview",
  chartRange: 20,
  market: null,
  stocks: [],
  etfs: [],
  currentDetailSymbol: "2330",
  currentDetailIsEtf: false,
};

// ===== 本機偽即時設定（尚未部署 Worker 前維持 enabled: false，部署後填入網址並改為 true）=====
const RT_CONFIG = {
  enabled: false,
  workerBaseUrl: "https://your-worker.workers.dev",
  pollIntervalMs: 20000,
};

let rtMarketTimer = null;
let rtQuoteTimer = null;
let latestRtQuoteData = null;

function fmtPct(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "--";
  const n = Number(val);
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

function fmtNum(val, dec = 2) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "--";
  const n = Number(val);
  return Number.isInteger(n) ? String(n) : n.toFixed(dec);
}

function fmtSignedNum(val, suffix = "") {
  if (val === null || val === undefined || val === "" || Number.isNaN(Number(val))) return "--";
  const n = Number(val);
  return `${n > 0 ? "+" : ""}${n}${suffix}`;
}

function pctClass(val) {
  const n = Number(val);
  if (n > 0) return "up";
  if (n < 0) return "down";
  return "flat";
}

function getRsiHint(rsiVal) {
  const n = Number(rsiVal);
  if (Number.isNaN(n)) return "動能待觀察";
  if (n >= 70) return "短線偏超買（留意過熱與拉回）";
  if (n <= 30) return "短線偏超賣（留意止跌與分批節奏）";
  if (n >= 50) return "多方動能佔上風";
  return "空方動能偏弱";
}

function getMaPositionText(closeVal, maVal, label) {
  const c = Number(closeVal);
  const m = Number(maVal);
  if (Number.isNaN(c) || Number.isNaN(m) || m === 0) return `${label}：--`;
  const diffPct = ((c - m) / m) * 100;
  const side = c >= m ? "站上" : "跌破";
  return `${label} ${fmtNum(m, 2)}（${side} ${fmtPct(diffPct)}）`;
}

function syncDetailTabLabel(symbol = "2330", isEtf = false) {
  const tabBtn = document.getElementById("tab-btn-detail");
  if (!tabBtn) return;
  tabBtn.textContent = isEtf ? `2. ${symbol} / ETF追蹤` : `2. ${symbol} / 30指標`;
}

function switchPage(pageName) {
  appState.activePage = pageName;
  const tooltip = document.getElementById("chart-tooltip");
  if (pageName !== "overview" && tooltip) {
    tooltip.classList.add("hidden");
  }

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.page === pageName);
  });
  document.querySelectorAll(".page-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.id !== `page-${pageName}`);
  });

  if (pageName === "overview") {
    drawMarketTrendChart();
    refreshMarketRealtime();
  } else if (pageName === "screener") {
    renderStocks();
  } else if (pageName === "etf") {
    renderEtfs();
  } else if (pageName === "detail") {
    loadStock30Page(appState.currentDetailSymbol || "2330");
  }
}

function resolveSymbolInput(rawInput) {
  const q = String(rawInput || "").trim();
  if (!q) return "2330";
  const exactCode = q.match(/\b(\d{4,6}[A-Za-z0-9]*)\b/);
  if (exactCode) return exactCode[1].toUpperCase();

  const matched = appState.stocks.find(
    (s) => String(s.symbol).toLowerCase() === q.toLowerCase() || String(s.name).includes(q)
  );
  if (matched) return matched.symbol;

  const matchedEtf = appState.etfs.find(
    (e) => String(e.symbol).toLowerCase() === q.toLowerCase() || String(e.name).includes(q)
  );
  if (matchedEtf) return matchedEtf.symbol;

  return q.toUpperCase();
}

/* ================= 第 1 頁：大盤總覽與中間走勢圖 ================= */
function renderOverview() {
  const m = appState.market || {};
  const tw = m.tw_index || {};
  const otc = m.otc_index || m.tp_index || {};
  const inst = m.institutional_flow || {};
  const breadth = m.market_breadth || {};

  const validCount = appState.stocks.filter((s) => !s.excluded).length;
  const highCount = appState.stocks.filter((s) => !s.excluded && (s.total_score || 0) >= 22).length;
  const excludedCount = appState.stocks.filter((s) => s.excluded).length;

  const foreignVal = inst.foreign_net ?? inst.foreign;
  const trustVal = inst.trust_net ?? inst.trust;
  const dealerVal = inst.dealer_net ?? inst.dealer;

  const upVal = breadth.advancing ?? breadth.up_count ?? "--";
  const downVal = breadth.declining ?? breadth.down_count ?? "--";

  const kpiHtml = `
    <div class="kpi-card">
      <div class="kpi-top"><span>大盤多空燈號</span><span class="mini-pill">即時狀態</span></div>
      <div class="kpi-main accent-text">${m.market_mood || (validCount > 0 ? "多空分歧觀察" : "資料待補齊")}</div>
      <div class="kpi-sub">${m.market_note || `有效評分 ${validCount} 檔｜缺漏排除 ${excludedCount} 檔`}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-top"><span>加權 / 櫃買指數</span><span class="kpi-muted" id="rt-index-badge">大盤水位</span></div>
      <div class="kpi-main ${pctClass(tw.change_pct)}" id="rt-tw-main">${tw.close ?? "--"} <small>(${fmtPct(tw.change_pct)})</small></div>
      <div class="kpi-sub" id="rt-otc-sub">櫃買：${otc.close ?? "--"} (${fmtPct(otc.change_pct)})</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-top"><span>三大法人買賣超 (億元)</span><span class="kpi-muted">合計</span></div>
      <div class="kpi-main ${pctClass(inst.total_net)}">${inst.total_net !== undefined && inst.total_net !== null ? `${Number(inst.total_net) > 0 ? "+" : ""}${inst.total_net}` : "--"}</div>
      <div class="kpi-sub">外資 ${fmtSignedNum(foreignVal)}・投信 ${fmtSignedNum(trustVal)}・自營 ${fmtSignedNum(dealerVal)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-top"><span>市場 breadth 與高分家數</span><span class="kpi-muted" id="rt-breadth-badge">≥22分強勢</span></div>
      <div class="kpi-main" id="rt-breadth-main">
        <span class="up">漲 ${upVal}</span> / <span class="down">跌 ${downVal}</span>
      </div>
      <div class="kpi-sub">高分 ≥22 分：${breadth.high_score_count ?? highCount} 檔・排除：${breadth.excluded_count ?? excludedCount} 檔</div>
    </div>
  `;
  document.getElementById("overview-kpis").innerHTML = kpiHtml;

  const usList = Array.isArray(m.us_indices) && m.us_indices.length > 0
    ? m.us_indices
    : [
        { name: "道瓊工業", symbol: "^DJI", close: m.us?.dji?.close, change_pct: m.us?.dji?.change_pct, tag: "美股參考" },
        { name: "標普 500", symbol: "^GSPC", close: m.us?.sp500?.close, change_pct: m.us?.sp500?.change_pct, tag: "美股參考" },
        { name: "那斯達克", symbol: "^IXIC", close: m.us?.ixic?.close, change_pct: m.us?.ixic?.change_pct, tag: "科技風向" },
        { name: "費城半導體", symbol: "^SOX", close: m.us?.sox?.close, change_pct: m.us?.sox?.change_pct, tag: "2330 連動" },
      ];

  document.getElementById("us-indices-grid").innerHTML = usList
    .map(
      (u) => `
      <div class="us-box">
        <div class="us-head">
          <span>${u.name || u.symbol} (${u.symbol || ""})</span>
          <span class="us-tag">${u.tag || "美股參考"}</span>
        </div>
        <div class="us-val ${pctClass(u.change_pct)}">${u.close ?? "--"} <small>(${fmtPct(u.change_pct)})</small></div>
        <div class="us-sub">漲跌點：${u.change_pts !== undefined && u.change_pts !== null ? `${Number(u.change_pts) > 0 ? "+" : ""}${u.change_pts}` : "--"}</div>
      </div>
    `
    )
    .join("");

  drawMarketTrendChart();
  refreshMarketRealtime();
}

function getTrendSeries() {
  const m = appState.market || {};
  const rawSeries = m.history || m.series || m.trend_series || m.daily || [];
  if (Array.isArray(rawSeries) && rawSeries.length > 0) {
    return rawSeries.slice(-appState.chartRange).map((d, i) => ({
      date: d.date || d.label || `D-${rawSeries.length - i}`,
      indexVal: Number(d.close ?? d.twii ?? d.index ?? 0),
      flowVal: Number(d.institutional_net ?? d.inst_net ?? d.flow ?? 0),
    }));
  }

  const baseIdx = Number(m.tw_index?.close) || 23000;
  const lastFlow = Number(m.institutional_flow?.total_net) || 0;
  const len = appState.chartRange;
  const out = [];
  for (let i = 0; i < len; i++) {
    const progress = (i - len / 2) / len;
    const wave = Math.sin(i * 0.65) * 140 + progress * 260;
    const flowWave = i === len - 1 ? lastFlow : Math.round(Math.cos(i * 0.85) * 120);
    out.push({
      date: `T-${len - 1 - i}`,
      indexVal: Math.round((baseIdx + wave) * 10) / 10,
      flowVal: flowWave,
    });
  }
  return out;
}

function drawMarketTrendChart() {
  const tooltip = document.getElementById("chart-tooltip");
  if (appState.activePage !== "overview") {
    if (tooltip) tooltip.classList.add("hidden");
    return;
  }

  const canvas = document.getElementById("market-trend-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const series = getTrendSeries();

  const W = canvas.width;
  const H = canvas.height;
  const pad = { top: 24, right: 64, bottom: 34, left: 68 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;

  ctx.clearRect(0, 0, W, H);

  if (!series.length) return;

  const idxVals = series.map((s) => s.indexVal);
  const flowVals = series.map((s) => s.flowVal);

  const minIdx = Math.min(...idxVals);
  const maxIdx = Math.max(...idxVals);
  const idxSpan = Math.max(maxIdx - minIdx, 50);
  const yIdxMin = minIdx - idxSpan * 0.12;
  const yIdxMax = maxIdx + idxSpan * 0.12;

  const maxAbsFlow = Math.max(...flowVals.map((v) => Math.abs(v)), 100);

  ctx.strokeStyle = "rgba(148, 163, 184, 0.14)";
  ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) {
    const y = pad.top + (plotH * g) / 4;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(W - pad.right, y);
    ctx.stroke();

    const tickVal = yIdxMax - ((yIdxMax - yIdxMin) * g) / 4;
    ctx.fillStyle = "#94a3b8";
    ctx.font = "11px sans-serif";
    ctx.textAlign = "right";
    ctx.fillText(tickVal.toFixed(0), pad.left - 8, y + 4);
  }

  ctx.fillStyle = "#94a3b8";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "left";
  ctx.fillText(`+${maxAbsFlow.toFixed(0)}`, W - pad.right + 8, pad.top + 6);
  ctx.fillText("0", W - pad.right + 8, pad.top + plotH / 2 + 4);
  ctx.fillText(`-${maxAbsFlow.toFixed(0)}`, W - pad.right + 8, pad.top + plotH);

  const stepX = plotW / series.length;
  const barW = Math.max(6, Math.min(22, stepX * 0.45));
  const zeroY = pad.top + plotH * 0.65;

  series.forEach((pt, i) => {
    const cx = pad.left + stepX * (i + 0.5);
    const barH = (Math.abs(pt.flowVal) / maxAbsFlow) * (plotH * 0.28);
    ctx.fillStyle = pt.flowVal >= 0 ? "rgba(239, 68, 68, 0.72)" : "rgba(34, 197, 94, 0.72)";
    if (pt.flowVal >= 0) {
      ctx.fillRect(cx - barW / 2, zeroY - barH, barW, barH);
    } else {
      ctx.fillRect(cx - barW / 2, zeroY, barW, barH);
    }

    if (i === 0 || i === Math.floor(series.length / 2) || i === series.length - 1 || i % Math.ceil(series.length / 6) === 0) {
      ctx.fillStyle = "#94a3b8";
      ctx.font = "11px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(pt.date, cx, H - 10);
    }
  });

  ctx.strokeStyle = "#38bdf8";
  ctx.lineWidth = 2.4;
  ctx.beginPath();
  series.forEach((pt, i) => {
    const cx = pad.left + stepX * (i + 0.5);
    const ratio = (pt.indexVal - yIdxMin) / (yIdxMax - yIdxMin);
    const cy = pad.top + plotH - ratio * plotH;
    if (i === 0) ctx.moveTo(cx, cy);
    else ctx.lineTo(cx, cy);
  });
  ctx.stroke();

  series.forEach((pt, i) => {
    const cx = pad.left + stepX * (i + 0.5);
    const ratio = (pt.indexVal - yIdxMin) / (yIdxMax - yIdxMin);
    const cy = pad.top + plotH - ratio * plotH;
    ctx.fillStyle = "#38bdf8";
    ctx.beginPath();
    ctx.arc(cx, cy, 3, 0, Math.PI * 2);
    ctx.fill();
  });

  canvas.onmousemove = (ev) => {
    if (appState.activePage !== "overview" || !tooltip) {
      if (tooltip) tooltip.classList.add("hidden");
      return;
    }
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const scaleX = canvas.width / rect.width;
    const x = (ev.clientX - rect.left) * scaleX;
    if (x < pad.left || x > W - pad.right) {
      tooltip.classList.add("hidden");
      return;
    }
    const idx = Math.min(series.length - 1, Math.max(0, Math.floor((x - pad.left) / stepX)));
    const item = series[idx];
    tooltip.innerHTML = `<strong>${item.date}</strong><br/>加權指數：${item.indexVal}<br/>三大法人：${item.flowVal > 0 ? "+" : ""}${item.flowVal} 億`;
    tooltip.style.left = `${Math.min(rect.width - 150, Math.max(10, (ev.clientX - rect.left) + 12))}px`;
    tooltip.style.top = `${Math.max(10, (ev.clientY - rect.top) - 48)}px`;
    tooltip.classList.remove("hidden");
  };
  canvas.onmouseleave = () => {
    if (tooltip) tooltip.classList.add("hidden");
  };
}

/* ================= 第 2 頁：個股 30 指標 / ETF 獨立追蹤面板 ================= */
function isMissingVal(v) {
  if (v === null || v === undefined) return true;
  if (typeof v === "string") {
    const s = v.trim().toLowerCase();
    return s === "" || s === "缺漏" || s === "n/a" || s === "na" || s === "--" || s === "null" || s === "none";
  }
  if (typeof v === "number") return Number.isNaN(v);
  return false;
}

function renderRtQuoteInlineHtml(symbol) {
  const q = latestRtQuoteData?.quotes?.[symbol];
  if (!RT_CONFIG.enabled || !q || q.price === null || q.price === undefined) {
    return `<span id="rt-stock-quote" style="display:none;"></span><span id="rt-stock-status"></span>`;
  }
  const color = q.diff > 0 ? "#ef4444" : q.diff < 0 ? "#22c55e" : "#94a3b8";
  const signDiff = q.diff > 0 ? `▲ +${q.diff.toFixed(2)}` : q.diff < 0 ? `▼ ${q.diff.toFixed(2)}` : "0.00";
  const signPct = q.diffPct > 0 ? `+${q.diffPct.toFixed(2)}%` : `${q.diffPct?.toFixed(2) ?? "--"}%`;
  const statusTxt = latestRtQuoteData.isTradingHours
    ? `● 盤中 ${q.time || latestRtQuoteData.updatedAt || ""}`
    : `即時/最新 ${q.time || ""}`;

  return `
    <span id="rt-stock-quote" style="display:inline-flex;align-items:center;gap:6px;padding:2px 8px;border-radius:6px;font-size:15px;font-weight:700;background:rgba(255,255,255,0.06);color:${color};">
      ${fmtNum(q.price, 2)} <span>${signDiff} (${signPct})</span>
    </span>
    <span id="rt-stock-status" style="font-size:12px;color:${latestRtQuoteData.isTradingHours ? "#4ade80" : "#94a3b8"};">${statusTxt}</span>
  `;
}

function renderEtfDetailPanel(symbol, detail, etfSummaryRow) {
  const q = detail?.quote || etfSummaryRow || {};
  const name = detail?.name || q.name || symbol;
  const trendStatus = q.trend_status || "--";
  const closeVal = q.close;
  const chg1d = q.change_pct;
  const chg5d = q.change_5d_pct;
  const chg20d = q.change_20d_pct;
  const ma5 = q.ma5;
  const ma20 = q.ma20;
  const ma60 = q.ma60;
  const rsi14 = q.rsi14;
  const inst5d = q.inst_5d_net;
  const conc5d = q.concentration_5d;
  const updatedAt = detail?.updated_at || q.updated_at || "--";
  const noticeText =
    detail?.notice ||
    `ETF 獨立追蹤模式：目前均線架構為「${trendStatus}」，RSI(14)=${fmtNum(rsi14, 1)}，近 20 日報酬 ${fmtPct(chg20d)}。ETF 不適用個股財報(EPS/ROE)與本益比評分。`;

  return `
    <div class="stock-hero-card">
      <div class="hero-top">
        <div>
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
            <h2 style="margin:0;">${symbol} ${name} <span class="mini-pill">ETF 獨立追蹤</span></h2>
            ${renderRtQuoteInlineHtml(symbol)}
          </div>
          <p class="hero-note">${noticeText}</p>
          <p class="hero-note">資料時間：${updatedAt}</p>
        </div>
        <div class="hero-total">${trendStatus}</div>
      </div>

      <div class="chip-metrics-6">
        <div class="cm-box"><span>最新收盤價</span><strong class="${pctClass(chg1d)}">${fmtNum(closeVal, 2)}</strong></div>
        <div class="cm-box"><span>單日漲跌幅</span><strong class="${pctClass(chg1d)}">${fmtPct(chg1d)}</strong></div>
        <div class="cm-box"><span>近 5 日報酬</span><strong class="${pctClass(chg5d)}">${fmtPct(chg5d)}</strong></div>
        <div class="cm-box"><span>近 20 日報酬</span><strong class="${pctClass(chg20d)}">${fmtPct(chg20d)}</strong></div>
        <div class="cm-box"><span>RSI (14)</span><strong>${fmtNum(rsi14, 1)}</strong></div>
        <div class="cm-box"><span>近 5 日法人買賣超</span><strong class="${pctClass(inst5d)}">${fmtSignedNum(inst5d, " 張")}</strong></div>
      </div>
    </div>

    <div class="thirty-grid">
      <div class="dim-card">
        <div class="dim-head">
          <h3>趨勢與均線結構</h3>
          <span class="dim-score">${trendStatus}</span>
        </div>
        <div class="dim-rows">
          <div class="dim-row"><span class="m-badge m-pass">MA</span><span class="r-idx">1.</span><span class="r-label">5 日均線位置</span><span class="r-val">${getMaPositionText(closeVal, ma5, "5MA")}</span><span class="r-pts">短線</span></div>
          <div class="dim-row"><span class="m-badge m-pass">MA</span><span class="r-idx">2.</span><span class="r-label">20 日均線位置</span><span class="r-val">${getMaPositionText(closeVal, ma20, "20MA")}</span><span class="r-pts">月線</span></div>
          <div class="dim-row"><span class="m-badge m-pass">MA</span><span class="r-idx">3.</span><span class="r-label">60 日均線位置</span><span class="r-val">${getMaPositionText(closeVal, ma60, "60MA")}</span><span class="r-pts">季線</span></div>
          <div class="dim-row"><span class="m-badge m-pass">架構</span><span class="r-idx">4.</span><span class="r-label">均線排列型態</span><span class="r-val v-pass">${trendStatus}</span><span class="r-pts">趨勢</span></div>
        </div>
      </div>

      <div class="dim-card">
        <div class="dim-head">
          <h3>短中長動能與強弱</h3>
          <span class="dim-score">RSI ${fmtNum(rsi14, 1)}</span>
        </div>
        <div class="dim-rows">
          <div class="dim-row"><span class="m-badge m-pass">1D</span><span class="r-idx">1.</span><span class="r-label">單日漲跌幅</span><span class="r-val ${pctClass(chg1d)}">${fmtPct(chg1d)}</span><span class="r-pts">當日</span></div>
          <div class="dim-row"><span class="m-badge m-pass">5D</span><span class="r-idx">2.</span><span class="r-label">近 5 日累積報酬</span><span class="r-val ${pctClass(chg5d)}">${fmtPct(chg5d)}</span><span class="r-pts">一週</span></div>
          <div class="dim-row"><span class="m-badge m-pass">20D</span><span class="r-idx">3.</span><span class="r-label">近 20 日累積報酬</span><span class="r-val ${pctClass(chg20d)}">${fmtPct(chg20d)}</span><span class="r-pts">一月</span></div>
          <div class="dim-row"><span class="m-badge m-pass">RSI</span><span class="r-idx">4.</span><span class="r-label">RSI(14) 強弱判讀</span><span class="r-val">${getRsiHint(rsi14)}</span><span class="r-pts">${fmtNum(rsi14, 1)}</span></div>
        </div>
      </div>

      <div class="dim-card">
        <div class="dim-head">
          <h3>籌碼與 ETF 追蹤說明</h3>
          <span class="dim-score">獨立模式</span>
        </div>
        <div class="dim-rows">
          <div class="dim-row"><span class="m-badge m-pass">籌碼</span><span class="r-idx">1.</span><span class="r-label">近 5 日法人買賣超</span><span class="r-val ${pctClass(inst5d)}">${fmtSignedNum(inst5d, " 張")}</span><span class="r-pts">5日</span></div>
          <div class="dim-row"><span class="m-badge m-pass">籌碼</span><span class="r-idx">2.</span><span class="r-label">5 日籌碼集中度</span><span class="r-val">${conc5d !== undefined && conc5d !== null ? `${fmtNum(conc5d, 2)}%` : "--"}</span><span class="r-pts">集中</span></div>
          <div class="dim-row"><span class="m-badge m-warn">i</span><span class="r-idx">3.</span><span class="r-label">財報與估值評分</span><span class="r-val">不適用 ETF</span><span class="r-pts">排除</span></div>
          <div class="dim-row"><span class="m-badge m-warn">i</span><span class="r-idx">4.</span><span class="r-label">資料來源</span><span class="r-val">ETF 獨立批次</span><span class="r-pts">ETF</span></div>
        </div>
      </div>
    </div>
  `;
}

function normalize30FromDetail(detail) {
  const checks = detail?.checks || {};
  const mapGroup = (arr, dimName) => {
    const list = Array.isArray(arr) ? arr.slice(0, 10) : [];
    const items = list.map((item, i) => {
      const label = item?.label || item?.name || `${dimName}指標 #${i + 1}`;
      const rawVal = item?.value ?? item?.actual ?? item?.raw;
      const missing = isMissingVal(rawVal);
      const passed = typeof item?.passed === "boolean" ? item.passed : null;
      const score =
        item?.score !== undefined && item?.score !== null
          ? Number(item.score)
          : passed !== null
            ? passed
              ? 1
              : 0
            : 0;
      return {
        label,
        valueText: missing ? "缺漏 (N/A)" : String(rawVal),
        missing,
        passed,
        score,
      };
    });
    while (items.length < 10) {
      items.push({
        label: `${dimName}指標 #${items.length + 1}`,
        valueText: "缺漏 (N/A)",
        missing: true,
        passed: null,
        score: 0,
      });
    }
    return items;
  };

  return {
    fundamental: mapGroup(checks.fundamental, "基本面"),
    technical: mapGroup(checks.technical, "技術面"),
    chip: mapGroup(checks.chip, "籌碼面"),
  };
}

function render30ColumnHtml(title, scoreVal, items) {
  const missingCount = items.filter((x) => x.missing).length;
  return `
    <div class="dim-card">
      <div class="dim-head">
        <h3>${title}（10 項）</h3>
        <span class="dim-score">${scoreVal ?? 0} / 10 分・缺漏 ${missingCount}/10</span>
      </div>
      <div class="dim-rows">
        ${items
          .map((it, idx) => {
            const mark = it.missing ? "!" : it.passed === true ? "✓" : "✕";
            const markCls = it.missing ? "m-warn" : it.passed === true ? "m-pass" : "m-fail";
            const valCls = it.missing ? "v-missing" : it.passed === true ? "v-pass" : "";
            return `
              <div class="dim-row">
                <span class="m-badge ${markCls}">${mark}</span>
                <span class="r-idx">${idx + 1}.</span>
                <span class="r-label" title="${it.label}">${it.label}</span>
                <span class="r-val ${valCls}">${it.valueText}</span>
                <span class="r-pts">${it.score} 分</span>
              </div>
            `;
          })
          .join("")}
      </div>
    </div>
  `;
}

async function fetchDetailJson(symbol) {
  const cleanSymbol = String(symbol || "").trim().toUpperCase();
  const url = `./data/details/${encodeURIComponent(cleanSymbol)}.json`;

  let res;
  try {
    res = await fetch(url, { cache: "no-store" });
  } catch (_) {
    throw new Error("無法連線至資料來源或本機伺服器");
  }

  if (!res.ok) {
    throw new Error(`尚未建立此代號的明細檔案（HTTP ${res.status}）`);
  }

  const rawText = await res.text();
  const trimmed = rawText.trim();

  if (!trimmed || trimmed.startsWith("<")) {
    throw new Error("尚無此代號的明細資料（可能尚未更新或已略過）");
  }

  try {
    return JSON.parse(trimmed);
  } catch (_) {
    throw new Error("明細資料格式異常，請重新執行批次更新後再查詢");
  }
}

async function loadStock30Page(symbolInput) {
  const symbol = resolveSymbolInput(symbolInput);
  appState.currentDetailSymbol = symbol;
  const container = document.getElementById("detail-page-content");
  container.innerHTML = `<div class="loading-box">正在讀取 ${symbol} 明細...</div>`;

  const etfRow = appState.etfs.find((e) => String(e.symbol).toUpperCase() === String(symbol).toUpperCase());

  try {
    const detail = await fetchDetailJson(symbol);

    if (detail?.asset_type === "ETF" || detail?.quote || etfRow) {
      appState.currentDetailIsEtf = true;
      syncDetailTabLabel(symbol, true);
      container.innerHTML = renderEtfDetailPanel(symbol, detail, etfRow);
      refreshQuoteRealtime(symbol);
      return;
    }

    appState.currentDetailIsEtf = false;
    syncDetailTabLabel(symbol, false);

    const { fundamental, technical, chip } = normalize30FromDetail(detail);
    const sc = detail.scores || {};
    const m6 = detail.metrics_6 || {};
    const sumRow = appState.stocks.find((s) => String(s.symbol) === String(symbol)) || {};

    const fScore = sc.fundamental ?? detail.fundamental_score ?? 0;
    const tScore = sc.technical ?? detail.technical_score ?? 0;
    const cScore = sc.chip ?? detail.chip_score ?? 0;
    const totalScore = detail.total_score ?? fScore + tScore + cScore;

    container.innerHTML = `
      <div class="stock-hero-card">
        <div class="hero-top">
          <div>
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
              <h2 style="margin:0;">${detail.symbol || symbol} ${detail.name || sumRow.name || ""}</h2>
              ${renderRtQuoteInlineHtml(symbol)}
            </div>
            <p class="hero-note">${detail.trading_note || detail.exclude_reason || "30 項多維度因子檢視（基本面 10 + 技術面 10 + 籌碼面 10）"}</p>
          </div>
          <div class="hero-total ${detail.excluded ? "is-excl" : ""}">
            ${detail.excluded ? "已排除" : `${totalScore} / 30`}
          </div>
        </div>
        <div class="chip-metrics-6">
          <div class="cm-box"><span>主力買賣超</span><strong>${m6.main_force_net ?? "--"} 張</strong></div>
          <div class="cm-box"><span>散戶買賣超</span><strong>${m6.retail_net ?? "--"} 張</strong></div>
          <div class="cm-box"><span>融資增減</span><strong>${m6.margin_net ?? "--"} 張</strong></div>
          <div class="cm-box"><span>5日籌碼集中度</span><strong>${m6.chip_concentration_5d !== undefined ? `${m6.chip_concentration_5d}%` : "--"}</strong></div>
          <div class="cm-box"><span>大戶持股比例</span><strong>${m6.major_holder_pct !== undefined ? `${m6.major_holder_pct}%` : "--"}</strong></div>
          <div class="cm-box"><span>散戶持股比例</span><strong>${m6.retail_holder_pct !== undefined ? `${m6.retail_holder_pct}%` : "--"}</strong></div>
        </div>
      </div>

      <div class="thirty-grid">
        ${render30ColumnHtml("基本面", fScore, fundamental)}
        ${render30ColumnHtml("技術面", tScore, technical)}
        ${render30ColumnHtml("籌碼面", cScore, chip)}
      </div>
    `;
    refreshQuoteRealtime(symbol);
  } catch (err) {
    if (etfRow) {
      appState.currentDetailIsEtf = true;
      syncDetailTabLabel(symbol, true);
      container.innerHTML = renderEtfDetailPanel(symbol, null, etfRow);
      refreshQuoteRealtime(symbol);
      return;
    }
    appState.currentDetailIsEtf = false;
    syncDetailTabLabel(symbol, false);
    const reason = err && err.message ? err.message : "暫時無法取得此代號資料";
    container.innerHTML = `
      <div class="warn-banner">
        <strong>查無 ${symbol} 明細：</strong>${reason}。請確認代號是否正確或改查其他股票與 ETF 代號。
      </div>
    `;
  }
}

function openStockInDetailPage(symbol) {
  appState.currentDetailSymbol = symbol;
  document.getElementById("detail-symbol-input").value = symbol;
  switchPage("detail");
}

/* ================= 第 3 頁：強勢高分榜 ================= */
function renderStocks() {
  const kw = (document.getElementById("stock-search")?.value || "").trim().toLowerCase();
  const filter = document.getElementById("stock-filter")?.value || "high";
  const sort = document.getElementById("stock-sort")?.value || "score_desc";

  let list = appState.stocks.filter((r) => {
    if (kw && !`${r.symbol} ${r.name}`.toLowerCase().includes(kw)) return false;
    if (filter === "valid" && r.excluded) return false;
    if (filter === "high" && (r.excluded || (r.total_score || 0) < 22)) return false;
    if (filter === "excluded" && !r.excluded) return false;
    return true;
  });

  list.sort((a, b) => {
    if (sort === "symbol_asc") return String(a.symbol).localeCompare(String(b.symbol));
    if (sort === "chg_desc") return (b.change_pct || -999) - (a.change_pct || -999);
    const sa = a.excluded ? -1 : (a.total_score || 0);
    const sb = b.excluded ? -1 : (b.total_score || 0);
    return sb - sa;
  });

  const shown = list.slice(0, 200);
  const excludedTotal = appState.stocks.filter((s) => s.excluded).length;

  document.getElementById("stock-stats-bar").textContent =
    `符合條件共 ${list.length} 檔（顯示前 ${shown.length} 筆｜全市場普通股 ${appState.stocks.length} 檔，目前缺漏排除 ${excludedTotal} 檔）`;

  document.getElementById("stock-tbody").innerHTML = shown
    .map(
      (r) => `
      <tr onclick="openStockInDetailPage('${r.symbol}')">
        <td><strong>${r.symbol}</strong></td>
        <td>${r.name}</td>
        <td class="num">${r.close ?? "--"}</td>
        <td class="num ${pctClass(r.change_pct)}">${fmtPct(r.change_pct)}</td>
        <td class="num"><strong>${r.excluded ? "排除" : `${r.total_score ?? 0} / 30`}</strong></td>
        <td class="num">${r.fundamental_score ?? 0} / 10</td>
        <td class="num">${r.technical_score ?? 0} / 10</td>
        <td class="num">${r.chip_score ?? 0} / 10</td>
        <td><span class="status-tag ${r.excluded ? "tag-warn" : "tag-ok"}">${r.excluded ? "資料缺漏排除" : (r.badge || "多維度評分")}</span></td>
        <td><button type="button" class="link-mini" onclick="event.stopPropagation(); openStockInDetailPage('${r.symbol}')">查看明細</button></td>
      </tr>
    `
    )
    .join("");

  document.getElementById("exclude-rule-banner").innerHTML =
    `<strong>缺漏值排除規則說明：</strong>資料尚未公布或欄位不齊全者會直接排除評分排行（目前缺漏排除 ${excludedTotal} 檔；點選任一列可跳至第 2 頁查看 30 項指標實際值與缺漏狀態）。`;
}

/* ================= 第 4 頁：ETF 專區 ================= */
function renderEtfs() {
  const kw = (document.getElementById("etf-search")?.value || "").trim().toLowerCase();
  const trendFilter = document.getElementById("etf-trend-filter")?.value || "all";
  const sort = document.getElementById("etf-sort")?.value || "symbol_asc";

  let list = appState.etfs.filter((r) => {
    if (kw && !`${r.symbol} ${r.name}`.toLowerCase().includes(kw)) return false;
    if (trendFilter !== "all" && r.trend_status !== trendFilter) return false;
    return true;
  });

  list.sort((a, b) => {
    if (sort === "chg_desc") return (b.change_pct || -999) - (a.change_pct || -999);
    if (sort === "chg20_desc") return (b.change_20d_pct || -999) - (a.change_20d_pct || -999);
    if (sort === "rsi_desc") return (b.rsi14 || 0) - (a.rsi14 || 0);
    return String(a.symbol).localeCompare(String(b.symbol));
  });

  document.getElementById("etf-stats-bar").textContent =
    `ETF 清單共 ${list.length} 檔（全市場收錄 ${appState.etfs.length} 檔）`;

  document.getElementById("etf-tbody").innerHTML = list
    .map(
      (r) => `
      <tr onclick="openStockInDetailPage('${r.symbol}')">
        <td><strong>${r.symbol}</strong></td>
        <td>${r.name}</td>
        <td class="num">${r.close ?? "--"}</td>
        <td class="num ${pctClass(r.change_pct)}">${fmtPct(r.change_pct)}</td>
        <td class="num ${pctClass(r.change_5d_pct)}">${fmtPct(r.change_5d_pct)}</td>
        <td class="num ${pctClass(r.change_20d_pct)}">${fmtPct(r.change_20d_pct)}</td>
        <td class="num muted-cell">${r.ma5 ?? "--"} / ${r.ma20 ?? "--"} / ${r.ma60 ?? "--"}</td>
        <td class="num">${r.rsi14 ?? "--"}</td>
        <td><span class="status-tag tag-ok">${r.trend_status || "--"}</span></td>
      </tr>
    `
    )
    .join("");
}

/* ================= Worker 偽即時輪詢（預設關閉，部署後開啟） ================= */
async function refreshMarketRealtime() {
  const indexBadge = document.getElementById("rt-index-badge");
  const breadthBadge = document.getElementById("rt-breadth-badge");
  if (!indexBadge) return;

  if (!RT_CONFIG.enabled || !RT_CONFIG.workerBaseUrl || RT_CONFIG.workerBaseUrl.includes("your-worker")) {
    indexBadge.textContent = "盤後/靜態資料";
    return;
  }

  try {
    const resp = await fetch(`${RT_CONFIG.workerBaseUrl}/api/market-rt`, { cache: "no-store" });
    if (!resp.ok) return;
    const data = await resp.json();

    const modeTag = data.isTradingHours
      ? `● 盤中即時 ${data.updatedAt || ""}`
      : `非盤中 · ${data.updatedAt || "最新"}`;
    indexBadge.textContent = modeTag;
    indexBadge.style.color = data.isTradingHours ? "#4ade80" : "";

    if (data.tse && data.tse.price !== null && data.tse.price !== undefined) {
      const twMain = document.getElementById("rt-tw-main");
      if (twMain) {
        twMain.className = `kpi-main ${pctClass(data.tse.diffPct)}`;
        twMain.innerHTML = `${fmtNum(data.tse.price, 2)} <small>(${fmtPct(data.tse.diffPct)})</small>`;
      }
    }
    if (data.otc && data.otc.price !== null && data.otc.price !== undefined) {
      const otcSub = document.getElementById("rt-otc-sub");
      if (otcSub) {
        otcSub.textContent = `櫃買：${fmtNum(data.otc.price, 2)} (${fmtPct(data.otc.diffPct)})`;
      }
    }
    if (data.breadth && data.breadth.up != null && data.breadth.down != null) {
      const bMain = document.getElementById("rt-breadth-main");
      if (bMain) {
        bMain.innerHTML = `<span class="up">漲 ${data.breadth.up}</span> / <span class="down">跌 ${data.breadth.down}</span>`;
      }
      if (breadthBadge) {
        breadthBadge.textContent = data.isTradingHours ? `● 盤中 ${data.updatedAt || ""}` : "≥22分強勢";
        breadthBadge.style.color = data.isTradingHours ? "#4ade80" : "";
      }
    }
  } catch (_) {
    // 靜默降級
  }
}

async function refreshQuoteRealtime(symbol = appState.currentDetailSymbol) {
  if (!symbol || !RT_CONFIG.enabled || !RT_CONFIG.workerBaseUrl || RT_CONFIG.workerBaseUrl.includes("your-worker")) {
    return;
  }

  try {
    const resp = await fetch(
      `${RT_CONFIG.workerBaseUrl}/api/quote-rt?codes=${encodeURIComponent(symbol)}`,
      { cache: "no-store" }
    );
    if (!resp.ok) return;
    const payload = await resp.json();
    latestRtQuoteData = payload;

    const quoteEl = document.getElementById("rt-stock-quote");
    const statusEl = document.getElementById("rt-stock-status");
    const q = payload?.quotes?.[symbol];
    if (!quoteEl || !q || q.price === null || q.price === undefined) return;

    const color = q.diff > 0 ? "#ef4444" : q.diff < 0 ? "#22c55e" : "#94a3b8";
    const signDiff = q.diff > 0 ? `▲ +${q.diff.toFixed(2)}` : q.diff < 0 ? `▼ ${q.diff.toFixed(2)}` : "0.00";
    const signPct = q.diffPct > 0 ? `+${q.diffPct.toFixed(2)}%` : `${q.diffPct?.toFixed(2) ?? "--"}%`;

    quoteEl.style.display = "inline-flex";
    quoteEl.style.alignItems = "center";
    quoteEl.style.gap = "6px";
    quoteEl.style.padding = "2px 8px";
    quoteEl.style.borderRadius = "6px";
    quoteEl.style.fontSize = "15px";
    quoteEl.style.fontWeight = "700";
    quoteEl.style.background = "rgba(255,255,255,0.06)";
    quoteEl.style.color = color;
    quoteEl.innerHTML = `${fmtNum(q.price, 2)} <span>${signDiff} (${signPct})</span>`;

    if (statusEl) {
      statusEl.textContent = payload.isTradingHours
        ? `● 盤中 ${q.time || payload.updatedAt || ""}`
        : `即時/最新 ${q.time || ""}`;
      statusEl.style.fontSize = "12px";
      statusEl.style.color = payload.isTradingHours ? "#4ade80" : "#94a3b8";
    }
  } catch (_) {
    // 靜默失敗
  }
}

function startRealtimeLoops() {
  if (rtMarketTimer) clearInterval(rtMarketTimer);
  if (rtQuoteTimer) clearInterval(rtQuoteTimer);

  refreshMarketRealtime();
  refreshQuoteRealtime(appState.currentDetailSymbol);

  if (!RT_CONFIG.enabled) return;

  rtMarketTimer = setInterval(() => {
    if (appState.activePage === "overview") refreshMarketRealtime();
  }, RT_CONFIG.pollIntervalMs);

  rtQuoteTimer = setInterval(() => {
    if (appState.activePage === "detail") refreshQuoteRealtime(appState.currentDetailSymbol);
  }, RT_CONFIG.pollIntervalMs);
}

/* ================= 初始化與事件綁定 ================= */
async function initDashboard() {
  try {
    const [mRes, sRes, eRes] = await Promise.all([
      fetch("./data/market.json").catch(() => null),
      fetch("./data/summary.json").catch(() => null),
      fetch("./data/etf_summary.json").catch(() => null),
    ]);

    if (mRes && mRes.ok) {
      appState.market = await mRes.json();
      document.getElementById("updated-at").textContent =
        `資料時間：${appState.market.updated_at || "--"}`;
    }
    if (sRes && sRes.ok) {
      const sJson = await sRes.json();
      appState.stocks = (sJson.stocks || []).filter((r) => !String(r.symbol).startsWith("00"));
    }
    if (eRes && eRes.ok) {
      const eJson = await eRes.json();
      appState.etfs = eJson.etfs || [];
    }
  } catch (e) {
    console.error("初始化失敗:", e);
  }

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchPage(btn.dataset.page));
  });

  document.querySelectorAll(".range-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      appState.chartRange = Number(btn.dataset.range) || 20;
      drawMarketTrendChart();
    });
  });

  const runQuickLookup = () => {
    const val = document.getElementById("quick-symbol-input").value;
    if (!val.trim()) {
      switchPage("detail");
      return;
    }
    document.getElementById("detail-symbol-input").value = val.trim();
    openStockInDetailPage(val.trim());
  };
  document.getElementById("quick-symbol-btn").addEventListener("click", runQuickLookup);
  document.getElementById("quick-symbol-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runQuickLookup();
  });

  ["stock-search", "stock-filter", "stock-sort"].forEach((id) => {
    document.getElementById(id)?.addEventListener("input", renderStocks);
    document.getElementById(id)?.addEventListener("change", renderStocks);
  });

  ["etf-search", "etf-trend-filter", "etf-sort"].forEach((id) => {
    document.getElementById(id)?.addEventListener("input", renderEtfs);
    document.getElementById(id)?.addEventListener("change", renderEtfs);
  });

  const runDetailSwitch = () => {
    const val = document.getElementById("detail-symbol-input").value;
    loadStock30Page(val);
  };
  document.getElementById("detail-symbol-btn").addEventListener("click", runDetailSwitch);
  document.getElementById("detail-symbol-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runDetailSwitch();
  });
  document.getElementById("detail-reset-2330").addEventListener("click", () => {
    document.getElementById("detail-symbol-input").value = "2330";
    loadStock30Page("2330");
  });
  document.querySelectorAll(".chip-btn").forEach((chip) => {
    chip.addEventListener("click", () => {
      const sym = chip.dataset.sym;
      document.getElementById("detail-symbol-input").value = sym;
      loadStock30Page(sym);
    });
  });

  renderOverview();
  renderStocks();
  renderEtfs();
  loadStock30Page("2330");
  startRealtimeLoops();
}

initDashboard();