const API = "";
const PAGE_SIZE = 50;
const LIVE_POLL_MS = 5000;
const DATA_POLL_MS = 15000;

const state = {
  book: null,
  symbol: null,
  tradeSeries: null,
  decisionsPage: 0,
  decisionsTotal: 0,
  charts: {},
};

function $(id) {
  return document.getElementById(id);
}

function setOverlay(id, mode, message) {
  const el = $(id);
  if (!el) return;
  if (mode === "hide") {
    el.hidden = true;
    el.classList.remove("error");
    return;
  }
  el.hidden = false;
  el.textContent = message;
  el.classList.toggle("error", mode === "error");
}

function fmtTime(ms) {
  return new Date(ms).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function numClass(value, { positiveGood = true } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return "num-blue";
  if (value === 0) return "num-blue";
  const good = positiveGood ? value > 0 : value < 0;
  return good ? "num-green" : "num-yellow";
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function loadMeta() {
  const meta = await getJSON(`${API}/api/meta`);
  const bookSelect = $("book-select");
  const symbolSelect = $("symbol-select");
  bookSelect.innerHTML = meta.books.map((b) => `<option value="${b}">${b}</option>`).join("");
  symbolSelect.innerHTML = meta.symbols.map((s) => `<option value="${s}">${s}</option>`).join("");
  state.book = meta.books[0] || null;
  state.symbol = meta.symbols[0] || null;
  state.tradeSeries = meta.trade_series || [];
  if (state.book) bookSelect.value = state.book;
  if (state.symbol) symbolSelect.value = state.symbol;

  bookSelect.addEventListener("change", () => {
    state.book = bookSelect.value;
    state.decisionsPage = 0;
    refreshAll();
  });
  symbolSelect.addEventListener("change", () => {
    state.symbol = symbolSelect.value;
    state.decisionsPage = 0;
    refreshAll();
  });
}

function destroyChart(key) {
  if (state.charts[key]) {
    state.charts[key].destroy();
    delete state.charts[key];
  }
}

async function loadEquity() {
  setOverlay("equity-state", "show", "Loading…");
  try {
    const points = await getJSON(
      `${API}/api/equity?book=${encodeURIComponent(state.book)}&symbol=${encodeURIComponent(state.symbol)}`
    );
    if (!points.length) {
      setOverlay("equity-state", "show", "No equity data for this book/symbol.");
      destroyChart("equity");
      return;
    }
    setOverlay("equity-state", "hide");
    destroyChart("equity");
    const ctx = $("equity-chart").getContext("2d");
    state.charts.equity = new Chart(ctx, {
      type: "line",
      data: {
        labels: points.map((p) => fmtTime(p.ts_ms)),
        datasets: [
          {
            label: "Equity",
            data: points.map((p) => p.equity),
            borderColor: "#4da3ff",
            backgroundColor: "rgba(77,163,255,0.1)",
            pointRadius: 0,
            borderWidth: 1.5,
            tension: 0.1,
            fill: true,
          },
        ],
      },
      options: baseChartOptions(),
    });
  } catch (err) {
    setOverlay("equity-state", "error", `Failed to load equity: ${err.message}`);
  }
}

async function loadVetoBreakdown() {
  setOverlay("veto-state", "show", "Loading…");
  try {
    const data = await getJSON(
      `${API}/api/veto-breakdown?book=${encodeURIComponent(state.book)}&symbol=${encodeURIComponent(state.symbol)}`
    );
    if (!data.breakdown.length) {
      setOverlay("veto-state", "show", "No vetoes recorded for this book/symbol.");
      destroyChart("veto");
      return;
    }
    setOverlay("veto-state", "hide");
    destroyChart("veto");
    const ctx = $("veto-chart").getContext("2d");
    state.charts.veto = new Chart(ctx, {
      type: "bar",
      data: {
        labels: data.breakdown.map((b) => b.category),
        datasets: [
          {
            label: "Vetoed decisions",
            data: data.breakdown.map((b) => b.count),
            backgroundColor: "#e0c341",
          },
        ],
      },
      options: {
        ...baseChartOptions(),
        indexAxis: "y",
        plugins: { legend: { display: false } },
      },
    });
  } catch (err) {
    setOverlay("veto-state", "error", `Failed to load breakdown: ${err.message}`);
  }
}

function pickTradeSeriesKey() {
  const sym = (state.symbol || "").replace("USDT", "").toLowerCase();
  const prefix = state.book === "perps" ? "perps" : "paper";
  const candidate = `${prefix}_${sym}`;
  if (state.tradeSeries.includes(candidate)) return candidate;
  return state.tradeSeries.find((k) => k.endsWith(`_${sym}`)) || null;
}

async function loadTradesAndPrice() {
  const key = pickTradeSeriesKey();
  setOverlay("trades-state", "show", "Loading…");
  setOverlay("price-state", "show", "Loading…");
  $("trades-body").innerHTML = "";

  if (!key) {
    setOverlay("trades-state", "show", "No trade log for this book/symbol.");
    setOverlay("price-state", "show", "No trades to overlay — pick a symbol with a trade log.");
    destroyChart("price");
    return;
  }

  let trades;
  try {
    const data = await getJSON(`${API}/api/trades?series=${encodeURIComponent(key)}`);
    trades = data.rows;
  } catch (err) {
    setOverlay("trades-state", "error", `Failed to load trades: ${err.message}`);
    setOverlay("price-state", "error", `Failed to load trades: ${err.message}`);
    return;
  }

  if (!trades.length) {
    setOverlay("trades-state", "show", "No trades logged yet for this series.");
    setOverlay("price-state", "show", "No trades to overlay.");
    destroyChart("price");
    return;
  }

  setOverlay("trades-state", "hide");
  $("trades-body").innerHTML = trades
    .slice()
    .reverse()
    .map((t) => {
      const pnl = t.realized_pnl ?? 0;
      const usd = t.usd ?? t.notional ?? 0;
      const qty = t.qty ?? 0;
      const sideLabel = t.action ? `${t.side} / ${t.action}` : t.side;
      const pnlClass = numClass(pnl);
      return `<tr>
        <td>${fmtTime(t.ts_ms)}</td>
        <td>${sideLabel}</td>
        <td>${t.price}</td>
        <td>${qty.toFixed(6)}</td>
        <td>${usd.toFixed(2)}</td>
        <td class="${pnlClass}">${pnl.toFixed(4)}</td>
      </tr>`;
    })
    .join("");

  await loadPriceChart(trades);
}

async function loadPriceChart(trades) {
  const symbol = state.symbol;
  const startMs = Math.min(...trades.map((t) => t.ts_ms));
  const endMs = Math.max(...trades.map((t) => t.ts_ms));
  const span = endMs - startMs;
  const interval = span > 1000 * 60 * 60 * 24 * 10 ? "4h" : span > 1000 * 60 * 60 * 24 * 2 ? "1h" : "15m";

  try {
    const url = `https://api.binance.com/api/v3/klines?symbol=${symbol}&interval=${interval}&startTime=${startMs - 3600000}&endTime=${endMs + 3600000}&limit=1000`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Binance ${res.status}`);
    const klines = await res.json();
    if (!klines.length) {
      setOverlay("price-state", "show", "Binance returned no candles for this range.");
      destroyChart("price");
      return;
    }
    setOverlay("price-state", "hide");
    destroyChart("price");

    const priceLabels = klines.map((k) => fmtTime(k[0]));
    const priceData = klines.map((k) => parseFloat(k[4]));

    const isEntry = (t) => t.side === "buy" || (t.action || "").startsWith("enter");
    const isExit = (t) => t.side === "sell" || t.action === "exited";
    const buys = trades.filter(isEntry);
    const sells = trades.filter(isExit);

    const nearestIndex = (ts) => {
      let best = 0;
      let bestDiff = Infinity;
      klines.forEach((k, i) => {
        const diff = Math.abs(k[0] - ts);
        if (diff < bestDiff) {
          bestDiff = diff;
          best = i;
        }
      });
      return best;
    };

    const ctx = $("price-chart").getContext("2d");
    state.charts.price = new Chart(ctx, {
      type: "line",
      data: {
        labels: priceLabels,
        datasets: [
          {
            label: `${symbol} price`,
            data: priceData,
            borderColor: "#7c8794",
            pointRadius: 0,
            borderWidth: 1.25,
            tension: 0.1,
          },
          {
            label: "Entry",
            type: "scatter",
            data: buys.map((t) => ({ x: nearestIndex(t.ts_ms), y: t.price })),
            backgroundColor: "#35d07f",
            pointRadius: 5,
            showLine: false,
          },
          {
            label: "Exit",
            type: "scatter",
            data: sells.map((t) => ({ x: nearestIndex(t.ts_ms), y: t.price })),
            backgroundColor: "#e0544a",
            pointRadius: 5,
            showLine: false,
          },
        ],
      },
      options: baseChartOptions(),
    });
  } catch (err) {
    setOverlay("price-state", "error", `Failed to load Binance price data: ${err.message}`);
  }
}

function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 0) return "bad timestamp (future-dated)";
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

async function loadHeartbeat() {
  const badge = $("heartbeat-badge");
  const detail = $("heartbeat-detail");
  try {
    const hb = await getJSON(`${API}/api/heartbeat`);
    badge.classList.remove("num-green", "num-blue", "num-yellow", "num-red", "pulse");
    if (hb.last_ts_ms === null) {
      badge.textContent = "NO DATA";
      badge.classList.add("num-yellow");
      detail.textContent = "heartbeat.jsonl missing or empty — daemon has never ticked";
    } else if (hb.stale) {
      badge.textContent = "STALE";
      badge.classList.add("num-red");
      detail.textContent = `last tick ${fmtAge(hb.age_s)} (${fmtTime(hb.last_ts_ms)}) — daemon likely not running`;
    } else {
      badge.textContent = "LIVE";
      badge.classList.add("num-green", "pulse");
      detail.textContent = `last tick ${fmtAge(hb.age_s)} (${fmtTime(hb.last_ts_ms)})`;
    }
  } catch (err) {
    badge.textContent = "ERROR";
    badge.classList.remove("num-green", "num-blue", "num-yellow", "pulse");
    badge.classList.add("num-red");
    detail.textContent = `Failed to load heartbeat: ${err.message}`;
  }
}

async function loadPnlSummary() {
  const equityEl = $("pnl-equity");
  const realizedEl = $("pnl-realized");
  const countEl = $("pnl-trade-count");
  const lastTradeEl = $("pnl-last-trade");
  const key = pickTradeSeriesKey();
  if (!key) {
    equityEl.textContent = "—";
    realizedEl.textContent = "—";
    countEl.textContent = "no trade series for this book/symbol";
    lastTradeEl.textContent = "—";
    return;
  }
  try {
    const data = await getJSON(
      `${API}/api/pnl-summary?series=${encodeURIComponent(key)}&book=${encodeURIComponent(
        state.book
      )}&symbol=${encodeURIComponent(state.symbol)}`
    );
    equityEl.textContent = data.latest_equity !== null ? data.latest_equity.toFixed(2) : "—";
    equityEl.className = `status-value ${numClass(data.latest_equity)}`;
    realizedEl.textContent = data.realized_pnl.toFixed(4);
    realizedEl.className = `status-value ${numClass(data.realized_pnl)}`;
    countEl.textContent = `${data.trade_count} trades`;
    lastTradeEl.textContent = data.last_trade_ts_ms ? fmtTime(data.last_trade_ts_ms) : "no trades yet";
  } catch (err) {
    equityEl.textContent = "ERR";
    realizedEl.textContent = "ERR";
    countEl.textContent = err.message;
    lastTradeEl.textContent = "—";
  }
}

async function loadDecisions() {
  setOverlay("decisions-state", "show", "Loading…");
  try {
    const offset = state.decisionsPage * PAGE_SIZE;
    const data = await getJSON(
      `${API}/api/decisions?book=${encodeURIComponent(state.book)}&symbol=${encodeURIComponent(
        state.symbol
      )}&limit=${PAGE_SIZE}&offset=${offset}`
    );
    state.decisionsTotal = data.total;
    if (!data.rows.length) {
      setOverlay("decisions-state", "show", "No decisions logged for this book/symbol.");
      $("decisions-body").innerHTML = "";
    } else {
      setOverlay("decisions-state", "hide");
      $("decisions-body").innerHTML = data.rows
        .map((d) => {
          const conf = d.verdict ? d.verdict.confidence : null;
          const confClass = conf === null ? "num-blue" : conf >= 0.6 ? "num-green" : conf >= 0.4 ? "num-blue" : "num-yellow";
          const confPulse = conf !== null && conf >= 0.6 ? "pulse" : "";
          return `<tr>
            <td>${fmtTime(d.ts_ms)}</td>
            <td>${d.book}</td>
            <td>${d.symbol}</td>
            <td>${d.action}</td>
            <td>${(d.vetoed_by || []).join(", ") || "—"}</td>
            <td class="${confClass} ${confPulse}">${conf === null ? "—" : conf.toFixed(2)}</td>
            <td>${d.equity !== null && d.equity !== undefined ? d.equity.toFixed(2) : "—"}</td>
          </tr>`;
        })
        .join("");
    }
    updatePager();
  } catch (err) {
    setOverlay("decisions-state", "error", `Failed to load decisions: ${err.message}`);
  }
}

function updatePager() {
  const totalPages = Math.max(1, Math.ceil(state.decisionsTotal / PAGE_SIZE));
  $("page-info").textContent = `Page ${state.decisionsPage + 1} of ${totalPages} (${state.decisionsTotal} decisions)`;
  $("prev-page").disabled = state.decisionsPage <= 0;
  $("next-page").disabled = state.decisionsPage + 1 >= totalPages;
}

function baseChartOptions() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    scales: {
      x: { ticks: { color: "#7c8794", maxTicksLimit: 8 }, grid: { color: "#232932" } },
      y: { ticks: { color: "#7c8794" }, grid: { color: "#232932" } },
    },
    plugins: {
      legend: { labels: { color: "#d7dde3" } },
    },
  };
}

function refreshAll() {
  loadEquity();
  loadVetoBreakdown();
  loadTradesAndPrice();
  loadDecisions();
  loadPnlSummary();
}

// Price chart overlay hits the public Binance klines API, so it stays on
// filter-change/manual refresh only. Heartbeat + P&L are local log reads —
// cheap to poll. Equity/veto/decisions are local too but redraw charts, so
// they get a slower interval to keep the UI calm.
function startPolling() {
  setInterval(() => {
    loadHeartbeat();
    loadPnlSummary();
  }, LIVE_POLL_MS);
  setInterval(() => {
    loadEquity();
    loadVetoBreakdown();
    loadDecisions();
  }, DATA_POLL_MS);
}

function wirePager() {
  $("prev-page").addEventListener("click", () => {
    if (state.decisionsPage > 0) {
      state.decisionsPage -= 1;
      loadDecisions();
    }
  });
  $("next-page").addEventListener("click", () => {
    const totalPages = Math.max(1, Math.ceil(state.decisionsTotal / PAGE_SIZE));
    if (state.decisionsPage + 1 < totalPages) {
      state.decisionsPage += 1;
      loadDecisions();
    }
  });
}

async function init() {
  wirePager();
  loadHeartbeat();
  try {
    await loadMeta();
    refreshAll();
    startPolling();
  } catch (err) {
    setOverlay("equity-state", "error", `Failed to load dashboard: ${err.message}`);
  }
}

init();
