// worker-rt-proxy.js（先放本機或合併進現有 Worker，尚未執行 wrangler deploy）
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function isMarketTradingHours(now = new Date()) {
  // 轉為台北時間 (UTC+8)
  const tw = new Date(now.toLocaleString("en-US", { timeZone: "Asia/Taipei" }));
  const day = tw.getDay(); // 0=日, 6=六
  if (day === 0 || day === 6) return false;
  const mins = tw.getHours() * 60 + tw.getMinutes();
  // 08:58 ~ 13:35 視為盤中即時視窗
  return mins >= 8 * 60 + 58 && mins <= 13 * 60 + 35;
}

function formatTwTime(now = new Date()) {
  return new Intl.DateTimeFormat("zh-TW", {
    timeZone: "Asia/Taipei",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(now);
}

function parseNum(val) {
  if (val === undefined || val === null || val === "-" || val === "") return null;
  const n = Number(String(val).replace(/,/g, "").trim());
  return Number.isFinite(n) ? n : null;
}

async function fetchMisQuotes(exChList) {
  const url = `https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=${encodeURIComponent(
    exChList.join("|")
  )}&_=${Date.now()}`;
  const resp = await fetch(url, {
    headers: {
      "User-Agent": "Mozilla/5.0",
      "Referer": "https://mis.twse.com.tw/stock/fibest.jsp",
    },
  });
  if (!resp.ok) throw new Error(`TWSE MIS HTTP ${resp.status}`);
  const data = await resp.json();
  return Array.isArray(data.msgArray) ? data.msgArray : [];
}

function normalizeQuoteItem(item) {
  const prevClose = parseNum(item.y);
  // z 為最新成交價；盤中尚未搓合或收盤後若 z 為 "-" 可 fallback 試算買賣均價或昨收
  let price = parseNum(item.z);
  if (price === null) {
    const b = parseNum(item.b?.split("_")?.[0]);
    const a = parseNum(item.a?.split("_")?.[0]);
    if (b !== null && a !== null) price = (b + a) / 2;
    else price = prevClose;
  }
  const diff = price !== null && prevClose !== null ? Number((price - prevClose).toFixed(2)) : null;
  const diffPct =
    diff !== null && prevClose ? Number(((diff / prevClose) * 100).toFixed(2)) : null;

  return {
    code: item.c,
    name: item.n,
    price,
    prevClose,
    diff,
    diffPct,
    volume: parseNum(item.v),
    time: item.t || "",
  };
}

async function handleMarketRt(request, ctx) {
  const cache = caches.default;
  const cacheKey = new Request(new URL(request.url).toString(), request);
  const cached = await cache.match(cacheKey);
  if (cached) return cached;

  const trading = isMarketTradingHours();
  const updatedAt = formatTwTime();

  // 1. 加權指數 (tse_t00.tw) 與 櫃買指數 (otc_o00.tw)
  const idxRows = await fetchMisQuotes(["tse_t00.tw", "otc_o00.tw"]);
  const idxMap = Object.fromEntries(idxRows.map((r) => [r.c, normalizeQuoteItem(r)]));

  // 2. 盤中全市場漲跌家數（抓取 TWSE 公開即時盤面統計，若失敗則回傳 null 讓前端降級）
  let upCount = null;
  let downCount = null;
  let flatCount = null;
  try {
    const statResp = await fetch(
      `https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?type=ALLBUT0999&response=json&_=${Date.now()}`,
      { headers: { "User-Agent": "Mozilla/5.0" } }
    );
    if (statResp.ok) {
      const statJson = await statResp.json();
      const tables = Array.isArray(statJson.tables) ? statJson.tables : [];
      const targetTable = tables.find(
        (t) => Array.isArray(t.data) && t.data.some((row) => String(row[1] || "").includes("上漲"))
      );
      if (targetTable) {
        // 依 TWSE 盤面統計列解析（實測時可依實際欄位索引微調）
        for (const row of targetTable.data) {
          const label = String(row[0] || row[1] || "");
          if (label.includes("上漲")) upCount = parseNum(row[2] ?? row[1]);
          if (label.includes("下跌")) downCount = parseNum(row[2] ?? row[1]);
          if (label.includes("持平")) flatCount = parseNum(row[2] ?? row[1]);
        }
      }
    }
  } catch (_) {
    // 靜默降級，前端會保留現有盤後 breadth 顯示
  }

  const payload = {
    isTradingHours: trading,
    updatedAt,
    tse: idxMap["t00"] || null,
    otc: idxMap["o00"] || null,
    breadth: {
      up: upCount,
      down: downCount,
      flat: flatCount,
    },
  };

  const ttl = trading ? 15 : 300;
  const response = new Response(JSON.stringify(payload), {
    status: 200,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": `public, max-age=${ttl}`,
      ...CORS_HEADERS,
    },
  });
  ctx.waitUntil(cache.put(cacheKey, response.clone()));
  return response;
}

async function handleQuoteRt(request, ctx) {
  const url = new URL(request.url);
  const rawCodes = (url.searchParams.get("codes") || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 20);

  if (!rawCodes.length) {
    return new Response(JSON.stringify({ quotes: {} }), {
      status: 200,
      headers: { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS },
    });
  }

  const cache = caches.default;
  const cacheKey = new Request(url.toString(), request);
  const cached = await cache.match(cacheKey);
  if (cached) return cached;

  const trading = isMarketTradingHours();
  const updatedAt = formatTwTime();

  // 先嘗試 tse_ 與 otc_ 雙前綴查詢，涵蓋上市/上櫃個股與 ETF
  const exChList = rawCodes.flatMap((code) => [`tse_${code}.tw`, `otc_${code}.tw`]);
  const rows = await fetchMisQuotes(exChList);

  const quotes = {};
  for (const row of rows) {
    if (!row.c || !row.n) continue;
    const norm = normalizeQuoteItem(row);
    // 若同一代號同時有回傳，優先保留有成交價或名稱非空的資料
    if (!quotes[norm.code] || (quotes[norm.code].price === null && norm.price !== null)) {
      quotes[norm.code] = norm;
    }
  }

  const payload = {
    isTradingHours: trading,
    updatedAt,
    quotes,
  };

  const ttl = trading ? 15 : 300;
  const response = new Response(JSON.stringify(payload), {
    status: 200,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": `public, max-age=${ttl}`,
      ...CORS_HEADERS,
    },
  });
  ctx.waitUntil(cache.put(cacheKey, response.clone()));
  return response;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    const { pathname } = new URL(request.url);
    if (pathname === "/api/market-rt") {
      return handleMarketRt(request, ctx);
    }
    if (pathname === "/api/quote-rt") {
      return handleQuoteRt(request, ctx);
    }
    return new Response("Not Found", { status: 404, headers: CORS_HEADERS });
  },
};