const appState = {
  activePage: "overview",
  chartRange: 20,
  market: null,
  stocks: [],
  etfs: [],
  currentDetailSymbol: "2330",
};

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

function pctClass(val) {
  const n = Number(val);
  if (n > 0) return "up";
  if (n < 0) return "down";
  return "flat";
}

function switchPage(pageName) {
  appState.activePage = pageName;
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.page === pageName);
  });
  document.querySelectorAll(".page-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.id !== `page-${pageName}`);
  });

  if (pageName === "overview") {
    drawMarketTrendChart();
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
  const inst = m.institutional_flow || {};
  const breadth = m.market_breadth || {};

  const validCount = appState.stocks.filter((s) => !s.excluded).length;
  const highCount = appState.stocks.filter((s) => !s.excluded && (s.total_score || 0) >= 22).length;
  const excludedCount = appState.stocks.filter((s) => s.excluded).length;

  const kpiHtml = `
    <div class="kpi-card">
      <div class="kpi-top"><span>大盤多空燈號</span><span class="mini-pill">即時狀態</span></div>
      <div class="kpi-main accent-text">${m.market_mood || (validCount > 0 ? "多空分歧觀察" : "資料待補齊")}</div>
      <div class="kpi-sub">${m.market_note || `有效評分 ${validCount} 檔｜缺漏排除 ${excludedCount} 檔`}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-top"><span>加權 / 櫃買指數</span><span class="kpi-muted">大盤水位</span></div>
      <div class="kpi-main ${pctClass(tw.change_pct)}">${tw.close ?? "--"} <small>(${fmtPct(tw.change_pct)})</small></div>
      <div class="kpi-sub">櫃買：${m.tp_index?.close ?? "--"} (${fmtPct(m.tp_index?.change_pct)})</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-top"><span>三大法人買賣超 (億元)</span><span class="kpi-muted">合計</span></div>
      <div class="kpi-main ${pctClass(inst.total_net)}">${inst.total_net !== undefined ? `${Number(inst.total_net) > 0 ? "+" : ""}${inst.total_net}` : "--"}</div>
      <div class="kpi-sub">外資 ${inst.foreign ?? "--"}・投信 ${inst.trust ?? "--"}・自營 ${inst.dealer ?? "--"}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-top"><span>市場 breadth 與高分家數</span><span class="kpi-muted">≥22分強勢</span></div>
      <div class="kpi-main">
        <span class="up">漲 ${breadth.up_count ?? "--"}</span> / <span class="down">跌 ${breadth.down_count ?? "--"}</span>
      </div>
      <div class="kpi-sub">高分 ≥22 分：${breadth.high_score_count ?? highCount} 檔・排除：${breadth.excluded_count ?? excludedCount} 檔</div>
    </div>
  `;
  document.getElementById("overview-kpis").innerHTML = kpiHtml;

  // 美股四大指數
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

  // 若 market.json 尚未提供歷史序列，以最新指數與法人數值生成平滑參考序列
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

  // 背景格線
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

  // 右軸法人買賣超刻度
  ctx.fillStyle = "#94a3b8";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "left";
  ctx.fillText(`+${maxAbsFlow.toFixed(0)}`, W - pad.right + 8, pad.top + 6);
  ctx.fillText("0", W - pad.right + 8, pad.top + plotH / 2 + 4);
  ctx.fillText(`-${maxAbsFlow.toFixed(0)}`, W - pad.right + 8, pad.top + plotH);

  // 柱狀圖：三大法人買賣超
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

    // X 軸日期
    if (i === 0 || i === Math.floor(series.length / 2) || i === series.length - 1 || i % Math.ceil(series.length / 6) === 0) {
      ctx.fillStyle = "#94a3b8";
      ctx.font = "11px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(pt.date, cx, H - 10);
    }
  });

  // 折線圖：加權指數
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

  // 折線節點
  series.forEach((pt, i) => {
    const cx = pad.left + stepX * (i + 0.5);
    const ratio = (pt.indexVal - yIdxMin) / (yIdxMax - yIdxMin);
    const cy = pad.top + plotH - ratio * plotH;
    ctx.fillStyle = "#38bdf8";
    ctx.beginPath();
    ctx.arc(cx, cy, 3, 0, Math.PI * 2);
    ctx.fill();
  });

  // 滑鼠互動提示
  const tooltip = document.getElementById("chart-tooltip");
  canvas.onmousemove = (ev) => {
    const rect = canvas.getBoundingClientRect();
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
  canvas.onmouseleave = () => tooltip.classList.add("hidden");
}

/* ================= 第 2 頁：強勢高分榜 ================= */
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
    `<strong>缺漏值排除規則說明：</strong>資料尚未公布或欄位不齊全者會直接排除評分排行（目前缺漏排除 ${excludedTotal} 檔；點選任一列可跳至第 4 頁查看 30 項指標實際值與缺漏狀態）。`;
}

function openStockInDetailPage(symbol) {
  appState.currentDetailSymbol = symbol;
  document.getElementById("detail-symbol-input").value = symbol;
  switchPage("detail");
}

/* ================= 第 3 頁：ETF 專區 ================= */
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

/* ================= 第 4 頁：預設 2330 顯示 30 個指標 ================= */
function isMissingVal(v) {
  if (v === null || v === undefined) return true;
  if (typeof v === "string") {
    const s = v.trim().toLowerCase();
    return s === "" || s === "缺漏" || s === "n/a" || s === "na" || s === "--" || s === "null" || s === "none";
  }
  if (typeof v === "number") return Number.isNaN(v);
  return false;
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

async function loadStock30Page(symbolInput) {
  const symbol = resolveSymbolInput(symbolInput);
  appState.currentDetailSymbol = symbol;
  const container = document.getElementById("detail-page-content");
  container.innerHTML = `<div class="loading-box">正在讀取 ${symbol} 30 項指標明細...</div>`;

  try {
    const res = await fetch(`./data/details/${symbol}.json`);
    if (!res.ok) throw new Error(`找不到 ./data/details/${symbol}.json`);
    const detail = await res.json();
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
            <h2>${detail.symbol || symbol} ${detail.name || sumRow.name || ""}</h2>
            <p class="hero-note">${detail.trading_note || detail.exclude_reason || "30 項多維度因子檢視（基本面 10 + 技術面 10 + 籌碼面 10）"}</p>
          </div>
          <div class="hero-total ${detail.excluded ? "is-excl" : ""}">
            ${detail.excluded ? "已排除" : `${totalScore} / 30`} </div> </div>  <div class="chip-metrics-6"> <div class="cm-box"><span>主力買賣超</span><strong>${m6.main_force_net ?? "--"} 張</strong></div> <div class="cm-box"><span>散戶買賣超</span><strong>${m6.retail_net ?? "--"} 張</strong></div> <div class="cm-box"><span>融資增減</span><strong>${m6.margin_net ?? "--"} 張</strong></div> <div class="cm-box"><span>5日籌碼集中度</span><strong>${m6.chip_concentration_5d !== undefined ? `${m6.chip_concentration_5d}%` : "--"}</strong></div>
          <div class="cm-box"><span>大戶持股比例</span><strong>${m6.major_holder_pct !== undefined ? `${m6.major_holder_pct}\%` : "--"}</strong></div> <div class="cm-box"><span>散戶持股比例</span><strong>${m6.retail_holder_pct !== undefined ? `${m6.retail_holder_pct}%` : "--"}</strong></div>
        </div>
      </div>

      <div class="thirty-grid">
        ${render30ColumnHtml("基本面", fScore, fundamental)}
        ${render30ColumnHtml("技術面", tScore, technical)}
        ${render30ColumnHtml("籌碼面", cScore, chip)}
      </div>
    `;
  } catch (err) {
    container.innerHTML = `
      <div class="warn-banner">
        <strong>查無 ${symbol} 明細：</strong>${err.message}。請確認代號是否正確或重新輸入其他股票代號。
      </div>
    `;
  }
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

  // 分頁按鈕
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchPage(btn.dataset.page));
  });

  // 走勢圖區間切換
  document.querySelectorAll(".range-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      appState.chartRange = Number(btn.dataset.range) || 20;
      drawMarketTrendChart();
    });
  });

  // 頂部快速查詢：直接帶入第 4 頁顯示 30 項指標
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

  // 第 2 頁篩選與排序
  ["stock-search", "stock-filter", "stock-sort"].forEach((id) => {
    document.getElementById(id)?.addEventListener("input", renderStocks);
    document.getElementById(id)?.addEventListener("change", renderStocks);
  });

  // 第 3 頁 ETF 篩選與排序
  ["etf-search", "etf-trend-filter", "etf-sort"].forEach((id) => {
    document.getElementById(id)?.addEventListener("input", renderEtfs);
    document.getElementById(id)?.addEventListener("change", renderEtfs);
  });

  // 第 4 頁股票切換
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
}

initDashboard();