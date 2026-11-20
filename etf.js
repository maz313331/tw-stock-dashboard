const etfState = {
  items: [],
};

function fmtPct(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "--";
  const n = Number(val);
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

function pctClass(val) {
  const n = Number(val);
  if (n > 0) return "up";
  if (n < 0) return "down";
  return "flat";
}

function trendTagClass(status) {
  if (!status) return "tag-neutral";
  if (status.includes("多頭")) return "tag-bull";
  if (status.includes("偏多")) return "tag-above";
  if (status.includes("空頭") || status.includes("偏弱")) return "tag-bear";
  return "tag-neutral";
}

async function initEtfPage() {
  try {
    const res = await fetch("./data/etf_summary.json");
    if (res.ok) {
      const data = await res.json();
      etfState.items = data.etfs || [];
      document.getElementById("etf-updated-at").textContent =
        `最後更新：${data.updated_at || "--"}｜收錄 ${etfState.items.length} 檔 ETF`;
    }
  } catch (err) {
    console.error("載入 etf_summary.json 失敗:", err);
  }

  ["etf-search", "etf-trend-filter", "etf-sort"].forEach((id) => {
    document.getElementById(id).addEventListener("input", renderEtfs);
  });

  document.getElementById("modal-close").addEventListener("click", () => {
    document.getElementById("detail-modal").classList.add("hidden");
  });
  document.getElementById("detail-modal").addEventListener("click", (e) => {
    if (e.target.id === "detail-modal") {
      document.getElementById("detail-modal").classList.add("hidden");
    }
  });

  renderEtfs();
}

function renderEtfs() {
  const kw = document.getElementById("etf-search").value.trim().toLowerCase();
  const trendFilter = document.getElementById("etf-trend-filter").value;
  const sort = document.getElementById("etf-sort").value;

  let list = etfState.items.filter((r) => {
    if (kw && !`${r.symbol} ${r.name}`.toLowerCase().includes(kw)) return false;
    if (trendFilter !== "all" && r.trend_status !== trendFilter) return false;
    return true;
  });

  list.sort((a, b) => {
    if (sort === "chg_desc") return (b.change_pct || -999) - (a.change_pct || -999);
    if (sort === "chg20_desc") return (b.change_20d_pct || -999) - (a.change_20d_pct || -999);
    if (sort === "rsi_desc") return (b.rsi14 || 0) - (a.rsi14 || 0);
    if (sort === "rsi_asc") return (a.rsi14 || 100) - (b.rsi14 || 100);
    return String(a.symbol).localeCompare(String(b.symbol));
  });

  const bullCount = etfState.items.filter((e) => e.trend_status === "多頭排列").length;
  document.getElementById("etf-stats-bar").textContent =
    `符合條件共 ${list.length} 檔 ETF（全市場收錄 ${etfState.items.length} 檔｜目前「多頭排列」共 ${bullCount} 檔）`;

  document.getElementById("etf-tbody").innerHTML = list
    .map(
      (r) => `
      <tr onclick="openEtfDetail('${r.symbol}', '${r.name}')">
        <td><strong>${r.symbol}</strong></td>
        <td>${r.name}</td>
        <td class="num">${r.close ?? "--"}</td>
        <td class="num ${pctClass(r.change_pct)}">${fmtPct(r.change_pct)}</td>
        <td class="num ${pctClass(r.change_5d_pct)}">${fmtPct(r.change_5d_pct)}</td>
        <td class="num ${pctClass(r.change_20d_pct)}">${fmtPct(r.change_20d_pct)}</td>
        <td class="num" style="font-size: 0.82rem; color: var(--muted);">
          ${r.ma5 ?? "--"} / ${r.ma20 ?? "--"} / ${r.ma60 ?? "--"}
        </td>
        <td class="num">${r.rsi14 ?? "--"}</td>
        <td><span class="tag ${trendTagClass(r.trend_status)}">${r.trend_status || "--"}</span></td>
      </tr>
    `
    )
    .join("");
}

async function openEtfDetail(symbol, name) {
  const modal = document.getElementById("detail-modal");
  const title = document.getElementById("modal-title");
  const body = document.getElementById("modal-body");

  title.textContent = `${symbol} ${name}`;
  body.innerHTML = `<p style="color: var(--muted);">載入明細中...</p>`;
  modal.classList.remove("hidden");

  try {
    const res = await fetch(`./data/details/${symbol}.json`);
    if (!res.ok) throw new Error("找不到明細檔");
    const detail = await res.json();
    const q = detail.quote || etfState.items.find((x) => x.symbol === symbol) || {};

    body.innerHTML = `
      <div class="notice-banner">${detail.notice || "ETF 獨立追蹤模式"}</div>
      <div class="detail-grid">
        <div class="detail-box"><div class="lbl">最新收盤</div><div class="val">${q.close ?? "--"}</div></div>
        <div class="detail-box"><div class="lbl">單日漲跌</div><div class="val ${pctClass(q.change_pct)}">${fmtPct(q.change_pct)}</div></div>
        <div class="detail-box"><div class="lbl">近 5 日報酬</div><div class="val ${pctClass(q.change_5d_pct)}">${fmtPct(q.change_5d_pct)}</div></div>
        <div class="detail-box"><div class="lbl">近 20 日報酬</div><div class="val ${pctClass(q.change_20d_pct)}">${fmtPct(q.change_20d_pct)}</div></div>
        <div class="detail-box"><div class="lbl">5MA / 20MA / 60MA</div><div class="val" style="font-size:0.9rem;">${q.ma5 ?? "--"} / ${q.ma20 ?? "--"} / ${q.ma60 ?? "--"}</div></div>
        <div class="detail-box"><div class="lbl">RSI(14)</div><div class="val">${q.rsi14 ?? "--"}</div></div>
      </div>
      <p style="font-size: 0.82rem; color: var(--muted);">資料時間：${detail.updated_at || "--"}</p>
    `;
  } catch (e) {
    body.innerHTML = `<p style="color: var(--up);">無法讀取明細：${e.message}</p>`;
  }
}

initEtfPage();