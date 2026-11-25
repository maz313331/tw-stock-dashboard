// worker-rt-proxy.js（Cloudflare Worker 即時報價代理：台股大盤 + 個股 + 美股四大指數）
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const MIS_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
  "Referer": "https://mis.twse.com.tw/stock/fibest.jsp",
  "Accept": "application/json, text/javascript, */*; q=0.01",
  "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
  "Cache-Control": "no-cache",
};

const US_INDEX_TARGETS = [
  { key: "dji", symbol: "^DJI", name: "道瓊工業", tag: "美股參考" },
  { key: "sp500", symbol: "^GSPC", name: "標普 500", tag: "美股參考" },
  { key: "ixic", symbol: "^IXIC", name: "那斯達克", tag: "科技風向" },
  { key: "sox", symbol: "^SOX", name: "費城半導體", tag: "2330 連動" },
];

function jsonResponse(payload, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...CORS_HEADERS,
      ...extraHeaders,
    },
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isMarketTradingHours(now = new Date()) {
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

function pickFirstPrice(rawVal) {
  if (rawVal === undefined || rawVal === null) return null;
  const firstPart = String(rawVal).split("_")[0];
  return parseNum(firstPart);
}

async function fetchMisQuotes(exChList, retries = 2) {
  if (!Array.isArray(exChList) || exChList.length === 0) {
    return { rows: [], error: null };
  }

  const url = `https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=${encodeURIComponent(
    exChList.join("|")
  )}&_=${Date.now()}`;

  let lastError = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const resp = await fetch(url, {
        headers: MIS_HEADERS,
        cf: { cacheTtl: 0, cacheEverything: false },
      });

      if (!resp.ok) {
        lastError = `TWSE MIS HTTP ${resp.status}`;
        if (attempt < retries) {
          await sleep(200 * (attempt + 1));
          continue;
        }
        return { rows: [], error: lastError };
      }

      const data = await resp.json();
      const rows = Array.isArray(data?.msgArray) ? data.msgArray : [];
      return { rows, error: null };
    } catch (err) {
      lastError = err?.message || String(err);
      if (attempt < retries) {
        await sleep(200 * (attempt + 1));
        continue;
      }
      return { rows: [], error: lastError };
    }
  }

  return { rows: [], error: lastError || "Unknown MIS error" };
}

function normalizeQuoteItem(item = {}) {
  const prevClose = parseNum(item.y);
  let price = parseNum(item.z);
  if (price === null) {
    const b = pickFirstPrice(item.b);
    const a = pickFirstPrice(item.a);
    if (b !== null && a !== null) price = Number(((b + a) / 2).toFixed(2));
    else price = prevClose;
  }

  const diff =
    price !== null && prevClose !== null ? Number((price - prevClose).toFixed(2)) : null;
  const diffPct =
    diff !== null && prevClose ? Number(((diff / prevClose) * 100).toFixed(2)) : null;

  return {
    code: item.c || "",
    name: item.n || "",
    price,
    prevClose,
    diff,
    diffPct,
    volume: parseNum(item.v),
    time: item.t || "",
  };
}

async function fetchSingleUsIndex(target) {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(
    target.symbol
  )}?interval=1d&range=10d&_=${Date.now()}`;

  try {
    const resp = await fetch(url, {
      headers: {
        "User-Agent": MIS_HEADERS["User-Agent"],
        "Accept": "application/json",
      },
      cf: { cacheTtl: 30, cacheEverything: true },
    });
    if (!resp.ok) return null;

    const data = await resp.json();
    const result = data?.chart?.result?.[0];
    const meta = result?.meta || {};
    const closes = (result?.indicators?.quote?.[0]?.close || []).filter(
      (v) => typeof v === "number" && Number.isFinite(v)
    );

    const close =
      parseNum(meta.regularMarketPrice) ??
      (closes.length > 0 ? Number(closes[closes.length - 1].toFixed(2)) : null);

    // 優先使用 previousClose；若缺少則以日K序列倒數第二根收盤價為昨收
    let prevClose = parseNum(meta.previousClose);
    if (prevClose === null && closes.length >= 2) {
      const lastBar = Number(closes[closes.length - 1].toFixed(2));
      const secondLastBar = Number(closes[closes.length - 2].toFixed(2));
      prevClose =
        close !== null && Math.abs(close - lastBar) < 0.0001
          ? secondLastBar
          : lastBar;
    }
    if (prevClose === null) {
      prevClose = parseNum(meta.chartPreviousClose);
    }

    if (close === null) return null;

    const changePts =
      prevClose !== null ? Number((close - prevClose).toFixed(2)) : null;
    const changePct =
      changePts !== null && prevClose
        ? Number(((changePts / prevClose) * 100).toFixed(2))
        : null;

    return {
      key: target.key,
      symbol: target.symbol,
      name: target.name,
      tag: target.tag,
      close: Number(close.toFixed(2)),
      prevClose: prevClose !== null ? Number(prevClose.toFixed(2)) : null,
      change_pts: changePts,
      change_pct: changePct,
      marketState: meta.marketState || "",
    };
  } catch (_) {
    return null;
  }
}

async function fetchUsIndices() {
  const entries = await Promise.all(
    US_INDEX_TARGETS.map(async (target) => [target.key, await fetchSingleUsIndex(target)])
  );
  const us = {};
  for (const [key, item] of entries) {
    if (item) us[key] = item;
  }
  return Object.keys(us).length > 0 ? us : null;
}

async function fetchMarketBreadth() {
  let up = null;
  let down = null;
  let flat = null;

  try {
    const statResp = await fetch(
      `https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?type=ALLBUT0999&response=json&_=${Date.now()}`,
      {
        headers: {
          "User-Agent": MIS_HEADERS["User-Agent"],
          "Referer": "https://www.twse.com.tw/",
        },
      }
    );
    if (statResp.ok) {
      const statJson = await statResp.json();
      const tables = Array.isArray(statJson?.tables) ? statJson.tables : [];
      const targetTable = tables.find(
        (t) =>
          Array.isArray(t?.data) &&
          t.data.some((row) => String(row?.[0] || row?.[1] || "").includes("上漲"))
      );
      if (targetTable) {
        for (const row of targetTable.data) {
          const label = String(row?.[0] || row?.[1] || "");
          const val = parseNum(row?.[2] ?? row?.[1]);
          if (label.includes("上漲") && val !== null) up = val;
          if (label.includes("下跌") && val !== null) down = val;
          if (label.includes("持平") && val !== null) flat = val;
        }
      }
    }
  } catch (_) {
    // 靜默降級
  }

  return { up, down, flat };
}

async function handleMarketRt(request, ctx) {
  try {
    const cache = caches.default;
    const cacheKey = new Request(new URL(request.url).toString(), request);
    const cached = await cache.match(cacheKey);
    if (cached) return cached;

    const trading = isMarketTradingHours();
    const updatedAt = formatTwTime();

    const [{ rows: idxRows, error: idxError }, breadth, us] = await Promise.all([
      fetchMisQuotes(["tse_t00.tw", "otc_o00.tw"]),
      fetchMarketBreadth(),
      fetchUsIndices(),
    ]);

    const idxMap = Object.fromEntries(
      idxRows.filter((r) => r?.c).map((r) => [r.c, normalizeQuoteItem(r)])
    );

    const tse = idxMap["t00"] || null;
    const otc = idxMap["o00"] || null;
    const hasValidData = Boolean(tse?.price !== null || otc?.price !== null || us);

    const payload = {
      isTradingHours: trading,
      updatedAt,
      tse,
      otc,
      breadth,
      us,
      ...(idxError && !hasValidData ? { error: idxError } : {}),
    };

    const ttl = trading ? 15 : 60;
    const response = jsonResponse(payload, 200, {
      "Cache-Control": hasValidData ? `public, max-age=${ttl}` : "no-store",
    });

    if (hasValidData && ctx?.waitUntil) {
      ctx.waitUntil(cache.put(cacheKey, response.clone()));
    }
    return response;
  } catch (err) {
    return jsonResponse(
      {
        isTradingHours: isMarketTradingHours(),
        updatedAt: formatTwTime(),
        tse: null,
        otc: null,
        breadth: { up: null, down: null, flat: null },
        us: null,
        error: err?.message || "handleMarketRt failed",
      },
      200,
      { "Cache-Control": "no-store" }
    );
  }
}

async function handleQuoteRt(request, ctx) {
  try {
    const url = new URL(request.url);
    const rawCodes = (url.searchParams.get("codes") || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
      .slice(0, 20);

    if (!rawCodes.length) {
      return jsonResponse({ quotes: {} }, 200, { "Cache-Control": "no-store" });
    }

    const cache = caches.default;
    const cacheKey = new Request(url.toString(), request);
    const cached = await cache.match(cacheKey);
    if (cached) return cached;

    const trading = isMarketTradingHours();
    const updatedAt = formatTwTime();

    const exChList = rawCodes.flatMap((code) => [`tse_${code}.tw`, `otc_${code}.tw`]);
    const { rows, error } = await fetchMisQuotes(exChList);

    const quotes = {};
    for (const row of rows) {
      if (!row?.c || !row?.n) continue;
      const norm = normalizeQuoteItem(row);
      if (!quotes[norm.code] || (quotes[norm.code].price === null && norm.price !== null)) {
        quotes[norm.code] = norm;
      }
    }

    const hasValidQuotes = Object.keys(quotes).length > 0;
    const payload = {
      isTradingHours: trading,
      updatedAt,
      quotes,
      ...(error && !hasValidQuotes ? { error } : {}),
    };

    const ttl = trading ? 15 : 300;
    const response = jsonResponse(payload, 200, {
      "Cache-Control": hasValidQuotes ? `public, max-age=${ttl}` : "no-store",
    });

    if (hasValidQuotes && ctx?.waitUntil) {
      ctx.waitUntil(cache.put(cacheKey, response.clone()));
    }
    return response;
  } catch (err) {
    return jsonResponse(
      {
        isTradingHours: isMarketTradingHours(),
        updatedAt: formatTwTime(),
        quotes: {},
        error: err?.message || "handleQuoteRt failed",
      },
      200,
      { "Cache-Control": "no-store" }
    );
  }
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