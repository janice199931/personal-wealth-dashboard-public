const data = {
  asOf: "Example data",
  updatedAt: "",
  fxNote: "美股以 1 USD = 31.000 TWD 換算",
  year: 2026,
  metrics: {
    totalAssets: 0,
  },
  dividends: [],
  investments: {
    tw: {
      title: "台股持倉",
      marketValue: 0,
      shares: "0 股",
      gain: 0,
      returnRate: "0.00%",
      cost: 0,
      updatedAt: "",
    },
    us: {
      title: "美股持倉",
      accountValueUsd: 0,
      accountValueTwd: 0,
      todayChangeUsd: 0,
      todayChangeRate: "0.00%",
      buyingPowerUsd: 0,
      holdings: [],
    },
  },
  assetPie: [
    { label: "台股", value: 0, color: "#e9a3ad" },
    { label: "美股", value: 0, color: "#f0bd76" },
    { label: "現金", value: 0, color: "#a8c4a0" },
    { label: "負債", value: 0, color: "#b8acd3" },
  ],
  monthly: [],
  assetTrend: [],
  transactions: [],
  accountBreakdown: {},
  currentMonthFinance: null,
  leveragedPullbackSignal: { state: "idle" },
  rebalancer: {
    leveragedValue: 0,
    hasLeveragedHolding: false,
  },
};

const colors = ["#e9a3ad", "#f0bd76", "#a8c4a0", "#b8acd3", "#9ec7dc", "#d7a7ad"];
const money = new Intl.NumberFormat("zh-TW", {
  style: "currency",
  currency: "TWD",
  maximumFractionDigits: 0,
});
const stockPriceTwd = new Intl.NumberFormat("zh-TW", {
  style: "currency",
  currency: "TWD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const number = new Intl.NumberFormat("zh-TW");
let usdToTwd = 31.451;
const AUTO_PRICE_UPDATE_KEY = "wealthDashboardLastAutoPriceUpdate";
const PRICE_AUTO_REFRESH_MS = 5 * 60 * 1000;
const BIRTH_DATE = new Date("1999-08-31T00:00:00+08:00");
const EMERGENCY_FUND_TARGET = 200000;
const MONTHLY_INVESTMENT_TARGET = 35000;
const LEVERAGED_TARGET_RATIO = 70;
const CASH_TARGET_RATIO = 30;
const REBALANCE_BAND = 5;
const DASHBOARD_CORE_CACHE_KEY = "wealthDashboardLastCore";
const PRICE_UPDATE_TIMEOUT_MS = 65000;
let dataStatus = null;
let priceUpdateInProgress = false;
let priceAutoRefreshTimer = null;

function fetchWithTimeout(url, options = {}, timeoutMs = 10000) {
  const controller = new AbortController();
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = window.setTimeout(() => {
      controller.abort();
      reject(new Error(`Request timeout: ${url}`));
    }, timeoutMs);
  });
  return Promise.race([fetch(url, { ...options, signal: controller.signal }), timeout]).finally(() => {
    window.clearTimeout(timer);
  });
}

function handleAuthExpired(response) {
  if (!response || response.status !== 401) return false;
  renderPriceUpdateNotice("登入已過期，請重新登入。");
  window.setTimeout(() => {
    window.location.href = "/login.html";
  }, 700);
  return true;
}

function readLastDashboardCore() {
  if (!window.localStorage) return null;
  try {
    return JSON.parse(window.localStorage.getItem(DASHBOARD_CORE_CACHE_KEY) || "null");
  } catch {
    window.localStorage.removeItem(DASHBOARD_CORE_CACHE_KEY);
    return null;
  }
}

function rememberDashboardCore(core) {
  if (!window.localStorage || !core?.portfolio) return;
  try {
    window.localStorage.setItem(DASHBOARD_CORE_CACHE_KEY, JSON.stringify(core));
  } catch {
    // Last-good data is only a safety net; live Supabase data remains the source of truth.
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setText(id, value) {
  document.getElementById(id).textContent = value;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function todayKey() {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  return formatter.format(new Date()).replaceAll("-", "");
}

function currentMonthKey() {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
  });
  return formatter.format(new Date());
}

function rocYear(year) {
  return Number(year) - 1911;
}

function parseDisplayDate(value) {
  const text = cleanUpdateTime(value ?? "");
  if (!text) return null;
  const match = text.match(/^(\d{2,4})[/-](\d{1,2})[/-](\d{1,2})(?:[ T](\d{1,2}):(\d{1,2}))?/);
  if (!match) return null;
  let year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < 1911) year += 1911;
  const date = new Date(year, month - 1, day, Number(match[4] || 0), Number(match[5] || 0));
  const valid = date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day;
  return valid ? date : null;
}

function formatRocDate(value, includeTime = false) {
  const date = value instanceof Date ? value : parseDisplayDate(value);
  if (!date || Number.isNaN(date.getTime())) return cleanUpdateTime(value ?? "");
  const year = rocYear(date.getFullYear());
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const dateText = `${year}/${month}/${day}`;
  if (!includeTime) return dateText;
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  return `${dateText} ${hour}:${minute}`;
}

function formatTaiwanDate(dateText) {
  const [year, month, day] = dateText.split("/").map((part) => Number(part));
  if (!year || !month || !day) return data.investments.tw.updatedAt;
  return `${year}/${String(month).padStart(2, "0")}/${String(day).padStart(2, "0")}`;
}

function formatTaiwanClock(date = new Date()) {
  return new Intl.DateTimeFormat("zh-TW", {
    timeZone: "Asia/Taipei",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatTaiwanUpdateTime(dateText, timestamp = new Date()) {
  return `${formatTaiwanDate(dateText)} ${formatTaiwanClock(timestamp)}`;
}

function cleanUpdateTime(value) {
  return String(value).replace(/\s*台灣證交所/g, "").trim();
}

function formatUpdateTime(value) {
  const text = cleanUpdateTime(value ?? "");
  if (!text) return "尚未更新";
  const date = text.includes("T") ? new Date(text) : parseDisplayDate(text) || new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  return formatRocDate(date, true);
}

function setSidebarUpdatedAt(value) {
  const target = document.getElementById("sidebarUpdatedAt");
  if (target) target.textContent = formatUpdateTime(value);
}

function syncTaiwanStockPrice(price, dateText, updatedAt) {
  const tw = data.investments.tw;
  const shares = parseShares(tw.shares);
  const marketValue = Math.round(price * shares);
  const gain = marketValue - tw.cost;
  tw.marketValue = marketValue;
  tw.gain = gain;
  tw.returnRate = percent(gain, tw.cost, 2);
  tw.updatedAt = cleanUpdateTime(updatedAt ?? formatTaiwanUpdateTime(dateText));
  const twAsset = data.assetPie.find((row) => row.label === "台股");
  if (twAsset) twAsset.value = marketValue;
}

async function refreshTaiwanStockPrice() {
  if (!window.fetch || !window.localStorage) return;
  const cacheKey = `twse-0050-${todayKey()}`;
  const cached = window.localStorage.getItem(cacheKey);
  if (cached) {
    const item = JSON.parse(cached);
    syncTaiwanStockPrice(item.price, item.date, item.updatedAt);
    render();
    return;
  }

  try {
    const url = `https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date=${todayKey()}&stockNo=0050&response=json`;
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error("TWSE request failed");
    const payload = await response.json();
    const latest = payload.data?.at(-1);
    if (!latest) throw new Error("TWSE data empty");
    const item = {
      date: latest[0],
      price: Number(String(latest[6]).replace(/,/g, "")),
      updatedAt: formatTaiwanUpdateTime(latest[0]),
    };
    if (!Number.isFinite(item.price)) throw new Error("TWSE price invalid");
    window.localStorage.setItem(cacheKey, JSON.stringify(item));
    syncTaiwanStockPrice(item.price, item.date, item.updatedAt);
    render();
  } catch (error) {
    console.info("保留目前台股資料，台灣證交所資料暫時無法更新。", error);
  }
}

function parseAmount(value) {
  if (typeof value === "number") return value;
  return Number(String(value).replace(/[^\d.-]/g, ""));
}

function parseShares(value) {
  return Number(String(value).replace(/[^\d.]/g, ""));
}

function assetValue(label) {
  return data.assetPie.find((row) => row.label === label)?.value ?? 0;
}

function monthlyFallback() {
  return { month: "Demo", income: 0, expense: 0, net: 0, savingsRate: 0 };
}

function monthlyMetricRows() {
  if (window.financeData?.years?.length) {
    const rows = applyDividendIncomeToFinanceYears(window.financeData.years)
      .flatMap((year) => year.months ?? [])
      .filter((month) => /^\d{4}-\d{2}$/.test(String(month.month || "")))
      .sort((a, b) => String(a.month).localeCompare(String(b.month)));
    if (rows.length) return rows;
  }
  return data.monthly.length ? data.monthly : [monthlyFallback(), monthlyFallback()];
}

function percent(value, total, digits = 1) {
  return total ? `${((value / total) * 100).toFixed(digits)}%` : "0.0%";
}

function compactPercent(value) {
  const numeric = Number.parseFloat(String(value).replace("%", ""));
  if (!Number.isFinite(numeric)) return "0%";
  return `${new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 2 }).format(numeric)}%`;
}

function signedMoney(value, formatter = money) {
  const formatted = formatter.format(Math.abs(value));
  return `${value >= 0 ? "+" : "-"}${formatted}`;
}

function signedPercent(value) {
  return `${value >= 0 ? "+" : ""}${Number(value).toFixed(1)}%`;
}

function gainTone(value) {
  return Number(value) >= 0 ? "gain-positive" : "gain-negative";
}

function formatDaysAsMonths(days) {
  const totalDays = Math.max(0, Math.round(Number(days) || 0));
  const months = Math.floor(totalDays / 30);
  const remainingDays = totalDays % 30;
  if (!months) return `${remainingDays} 天`;
  if (!remainingDays) return `${months} 個月`;
  return `${months} 個月 ${remainingDays} 天`;
}

function formatSharesValue(value) {
  return `${number.format(Number(value).toFixed(6).replace(/\.?0+$/, ""))} 股`;
}

function formatUsdValue(value) {
  return usd.format(Number(value));
}

function formatUsdDisplay(value) {
  return usd.format(Number(value));
}

function formatUsdSummary(value) {
  return `$${number.format(Math.round(Number(value) || 0))} USD`;
}

function formatTwdApproxFromUsd(value) {
  return `(約 ${money.format(Number(value) * usdToTwd)})`;
}

function formatTwdApprox(value) {
  return `(約 ${money.format(Number(value) || 0)})`;
}

function formatPlainTwdApproxFromUsd(value, showSign = false) {
  const twdValue = Math.round((Number(value) || 0) * usdToTwd);
  const sign = twdValue < 0 ? "-" : showSign && twdValue > 0 ? "+" : "";
  return `(約 ${sign}$${number.format(Math.abs(twdValue))})`;
}

function applyDividendData(dividends) {
  data.dividends = Array.isArray(dividends) ? dividends : [];
}

function applyTransactionData(transactions) {
  data.transactions = Array.isArray(transactions) ? transactions : [];
}

function dividendNetTwd(dividend) {
  const amount = Number(dividend.amount) || 0;
  return dividend.currency === "USD" ? amount * usdToTwd : amount;
}

function currentYearDividendIncome() {
  const currentYear = String(new Date().getFullYear());
  return data.dividends
    .filter((dividend) => String(dividend.date || "").slice(0, 4) === currentYear)
    .reduce((sum, dividend) => sum + dividendNetTwd(dividend), 0);
}

function dividendIncomeByMonth() {
  return data.dividends.reduce((months, dividend) => {
    const month = String(dividend.date || "").slice(0, 7);
    if (!/^\d{4}-\d{2}$/.test(month)) return months;
    months[month] = (months[month] || 0) + dividendNetTwd(dividend);
    return months;
  }, {});
}

function transactionInvestmentAmount(transaction) {
  const shares = Number(transaction.shares) || 0;
  const price = Number(transaction.price) || 0;
  const fee = Number(transaction.fee) || 0;
  const amount = shares * price + fee;
  return transaction.market === "US" ? amount * usdToTwd : amount;
}

function investmentAmountForPeriod(periodKey) {
  return data.transactions
    .filter((transaction) => {
      const action = String(transaction.action || "").toUpperCase();
      return action === "BUY" && String(transaction.date || "").startsWith(periodKey);
    })
    .reduce((sum, transaction) => sum + transactionInvestmentAmount(transaction), 0);
}

function currentYearKey() {
  return String(new Date().getFullYear());
}

function applyDividendIncomeToFinanceYears(years) {
  const monthlyDividends = dividendIncomeByMonth();
  return years.map((year) => {
    const months = (year.months ?? []).map((month) => {
      const investmentIncome = Math.round(monthlyDividends[month.month] || 0);
      const income = (Number(month.income) || 0) + investmentIncome;
      const expense = Number(month.expense) || 0;
      const net = (Number(month.net) || 0) + investmentIncome;
      const savingsRate = income ? Math.round((net / income) * 1000) / 10 : 0;
      return { ...month, investmentIncome, income, net, savingsRate };
    });
    const investmentIncome = months.reduce((sum, month) => sum + (month.investmentIncome || 0), 0);
    const income = months.reduce((sum, month) => sum + (Number(month.income) || 0), 0);
    const expense = months.reduce((sum, month) => sum + (Number(month.expense) || 0), 0);
    const net = months.reduce((sum, month) => sum + (Number(month.net) || 0), 0);
    const savingsRate = income ? Math.round((net / income) * 1000) / 10 : 0;
    return { ...year, months, investmentIncome, income, expense, net, savingsRate };
  });
}

function applyPortfolioData(portfolio, history = []) {
  if (!portfolio?.summary) return;
  const twMarket = portfolio.markets?.TW ?? {};
  const usMarket = portfolio.markets?.US ?? {};
  const byMarketValueDesc = (a, b) => Number(b.marketValueTWD ?? b.marketValue ?? 0) - Number(a.marketValueTWD ?? a.marketValue ?? 0);
  const twHoldings = (portfolio.holdings ?? []).filter((holding) => holding.market === "TW").sort(byMarketValueDesc);
  const usHoldings = (portfolio.holdings ?? []).filter((holding) => holding.market === "US").sort(byMarketValueDesc);
  const leveragedHolding = (portfolio.holdings ?? []).find((holding) => String(holding.symbol || "").toUpperCase() === "00685L");

  usdToTwd = Number(portfolio.fxRate) || usdToTwd;
  data.updatedAt = portfolio.updatedAt || "";
  data.fxNote = `美股以 1 USD = ${portfolio.fxRate} TWD 換算`;
  data.metrics.totalAssets = portfolio.summary.totalAssets;
  data.investments.tw = {
    ...data.investments.tw,
    title: twHoldings[0]?.name ? `${twHoldings[0].name} ${twHoldings[0].symbol}` : data.investments.tw.title,
    marketValue: twMarket.marketValue ?? 0,
    shares: twHoldings[0] ? formatSharesValue(twHoldings[0].shares) : data.investments.tw.shares,
    gain: twMarket.unrealizedGain ?? 0,
    returnRate: `${Number(twMarket.returnRate ?? 0).toFixed(2)}%`,
    cost: twMarket.cost ?? 0,
    updatedAt: twMarket.updatedAt || data.investments.tw.updatedAt,
    holdings: twHoldings.map((holding) => ({
      symbol: holding.symbol,
      title: `${holding.name} ${holding.symbol}`,
      shares: formatSharesValue(holding.shares),
      price: Number(holding.price),
      cost: Number(holding.averageCost),
      marketValue: Number(holding.marketValueTWD),
      gain: Number(holding.unrealizedGainTWD),
      returnRate: `${Number(holding.returnRate).toFixed(2)}%`,
    })),
  };

  data.investments.us = {
    ...data.investments.us,
    accountValueUsd: usMarket.marketValueUSD ?? 0,
    accountValueTwd: usMarket.marketValue ?? 0,
    costTwd: usMarket.cost ?? 0,
    gainTwd: usMarket.unrealizedGain ?? 0,
    buyingPowerUsd: usMarket.cashUSD ?? 0,
    updatedAt: usMarket.updatedAt || "2026/05/30 05:10",
    holdings: usHoldings.map((holding) => ({
      symbol: holding.symbol,
      shares: formatSharesValue(holding.shares),
      priceValue: Number(holding.price),
      price: formatUsdValue(holding.price),
      costValue: Number(holding.averageCost),
      cost: formatUsdValue(holding.averageCost),
      marketValue: Number(holding.marketValue),
      marketValueTwd: Number(holding.marketValueTWD),
      totalCostTwd: Number(holding.totalCostTWD),
      gainValue: Number(holding.unrealizedGain),
      gainTwd: Number(holding.unrealizedGainTWD),
      gain: signedMoney(holding.unrealizedGain, usd),
      returnRate: signedPercent(holding.returnRate),
    })),
  };

  data.assetPie = [
    { label: "台股", value: portfolio.allocation?.taiwanStocks ?? 0, color: "#e9a3ad" },
    { label: "美股", value: portfolio.allocation?.usStocks ?? 0, color: "#f0bd76" },
    { label: "現金", value: portfolio.allocation?.cash ?? 0, color: "#a8c4a0" },
    { label: "負債", value: portfolio.allocation?.debt ?? 0, color: "#b8acd3" },
  ];
  data.rebalancer = {
    leveragedValue: Number(leveragedHolding?.marketValueTWD ?? leveragedHolding?.marketValue ?? 0) || 0,
    hasLeveragedHolding: Boolean(leveragedHolding),
  };

  if (history.length) {
    data.assetTrend = history.map((row) => ({
      month: row.date,
      assets: row.netWorth,
    }));
  }
}

function applyAccountData(accounts = {}) {
  data.accountBreakdown = accounts.accountBreakdown && typeof accounts.accountBreakdown === "object"
    ? accounts.accountBreakdown
    : {};
}

function applyCurrentMonthFinance(month = null) {
  data.currentMonthFinance = month && typeof month === "object" ? month : null;
}

function fitCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const cssWidth = Math.max(1, rect.width);
  const cssHeight = Math.max(1, rect.height || Number(canvas.getAttribute("height")) || 240);
  canvas.width = Math.floor(cssWidth * ratio);
  canvas.height = Math.floor(cssHeight * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx, width: cssWidth, height: cssHeight };
}

function drawGrid(ctx, area, ticks = 4) {
  ctx.strokeStyle = cssVar("--chart-grid") || "rgba(148, 163, 184, 0.2)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= ticks; i++) {
    const y = area.top + (area.height / ticks) * i;
    ctx.beginPath();
    ctx.moveTo(area.left, y);
    ctx.lineTo(area.left + area.width, y);
    ctx.stroke();
  }
}

function drawBarChart(canvas, rows) {
  const { ctx, width, height } = fitCanvas(canvas);
  const area = { left: 44, top: 18, width: width - 54, height: height - 62 };
  const max = Math.max(...rows.flatMap((row) => [row.income, row.expense]));
  ctx.clearRect(0, 0, width, height);
  drawGrid(ctx, area);

  const groupWidth = area.width / rows.length;
  const barWidth = Math.max(5, groupWidth * 0.28);
  rows.forEach((row, index) => {
    const x = area.left + index * groupWidth + groupWidth * 0.2;
    const incomeH = (row.income / max) * area.height;
    const expenseH = (row.expense / max) * area.height;
    ctx.fillStyle = "#e9a3ad";
    ctx.fillRect(x, area.top + area.height - incomeH, barWidth, incomeH);
    ctx.fillStyle = "#f0bd76";
    ctx.fillRect(x + barWidth + 3, area.top + area.height - expenseH, barWidth, expenseH);

    if (index % 2 === 1) return;
    ctx.fillStyle = "#8b7a72";
    ctx.font = "11px system-ui";
    ctx.textAlign = "center";
    ctx.fillText(row.month.slice(5), x + barWidth, height - 20);
  });
}

function drawLineChart(canvas, rows) {
  const { ctx, width, height } = fitCanvas(canvas);
  const area = { left: 50, top: 18, width: width - 64, height: height - 62 };
  const values = rows.map((row) => row.assets);
  const max = Math.max(...values);
  const min = Math.min(...values);
  const span = Math.max(1, max - min);
  ctx.clearRect(0, 0, width, height);
  drawGrid(ctx, area);

  const points = rows.map((row, index) => ({
    x: area.left + (area.width / (rows.length - 1)) * index,
    y: area.top + area.height - ((row.assets - min) / span) * area.height,
  }));

  ctx.strokeStyle = "#e9a3ad";
  ctx.lineWidth = 3;
  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y);
    else ctx.lineTo(point.x, point.y);
  });
  ctx.stroke();

  ctx.fillStyle = "#b8acd3";
  points.forEach((point) => {
    ctx.beginPath();
    ctx.arc(point.x, point.y, 3.5, 0, Math.PI * 2);
    ctx.fill();
  });

  ctx.fillStyle = "#8b7a72";
  ctx.font = "11px system-ui";
  ctx.textAlign = "center";
  rows.forEach((row, index) => {
    if (index % 3 === 0 || index === rows.length - 1) {
      ctx.fillText(row.month.slice(2), points[index].x, height - 20);
    }
  });
}

function drawDonutChart(canvas, rows) {
  const { ctx, width, height } = fitCanvas(canvas);
  const total = rows.reduce((sum, row) => sum + row.value, 0);
  const radius = Math.min(width, height) * 0.34;
  const centerX = width / 2;
  const centerY = height / 2;
  let angle = -Math.PI / 2;
  ctx.clearRect(0, 0, width, height);

  rows.forEach((row, index) => {
    const slice = (row.value / total) * Math.PI * 2;
    ctx.beginPath();
    ctx.arc(centerX, centerY, radius, angle, angle + slice);
    ctx.arc(centerX, centerY, radius * 0.58, angle + slice, angle, true);
    ctx.closePath();
    ctx.fillStyle = colors[index % colors.length];
    ctx.fill();
    angle += slice;
  });

  ctx.fillStyle = "#4a3f3a";
  ctx.font = "700 20px system-ui";
  ctx.textAlign = "center";
  ctx.fillText(`${number.format(Math.round(total / 10000))}萬`, centerX, centerY - 2);
  ctx.fillStyle = "#8b7a72";
  ctx.font = "12px system-ui";
  ctx.fillText("合計", centerX, centerY + 18);
}

function renderBreakdown(targetId, rows) {
  const total = rows.reduce((sum, row) => sum + row.value, 0);
  document.getElementById(targetId).innerHTML = rows
    .map((row, index) => {
      const pct = total ? Math.round((row.value / total) * 100) : 0;
      return `<div class="list-row">
        <span><i class="swatch" style="background:${colors[index % colors.length]}"></i>${row.label}</span>
        <strong>${pct}%</strong>
      </div>`;
    })
    .join("");
}

function drawPieChart(canvas, rows) {
  const { ctx, width, height } = fitCanvas(canvas);
  const total = rows.reduce((sum, row) => sum + row.value, 0);
  const radius = Math.min(width, height) * 0.38;
  const centerX = width / 2;
  const centerY = height / 2;
  let angle = -Math.PI / 2;
  const slices = [];
  ctx.clearRect(0, 0, width, height);

  rows.forEach((row) => {
    const slice = (row.value / total) * Math.PI * 2;
    const startAngle = angle;
    const endAngle = angle + slice;
    ctx.beginPath();
    ctx.moveTo(centerX, centerY);
    ctx.arc(centerX, centerY, radius, startAngle, endAngle);
    ctx.closePath();
    ctx.fillStyle = row.color;
    ctx.fill();
    ctx.strokeStyle = cssVar("--surface");
    ctx.lineWidth = 2;
    ctx.stroke();
    slices.push({ row, startAngle, endAngle, pct: total ? (row.value / total) * 100 : 0 });
    angle = endAngle;
  });

  ctx.fillStyle = cssVar("--surface");
  ctx.beginPath();
  ctx.arc(centerX, centerY, radius * 0.48, 0, Math.PI * 2);
  ctx.fill();

  ctx.font = "800 11px system-ui";
  ctx.textBaseline = "middle";
  slices.forEach(({ row, startAngle, endAngle, pct }) => {
    const middle = (startAngle + endAngle) / 2;
    const outside = pct < 7;
    const labelRadius = radius * (outside ? 1.18 : 0.76);
    const x = centerX + Math.cos(middle) * labelRadius;
    const y = centerY + Math.sin(middle) * labelRadius;
    const label = `${row.label} ${pct.toFixed(1)}%`;

    if (outside) {
      const lineStartX = centerX + Math.cos(middle) * radius * 0.95;
      const lineStartY = centerY + Math.sin(middle) * radius * 0.95;
      const lineEndX = centerX + Math.cos(middle) * radius * 1.08;
      const lineEndY = centerY + Math.sin(middle) * radius * 1.08;
      ctx.beginPath();
      ctx.moveTo(lineStartX, lineStartY);
      ctx.lineTo(lineEndX, lineEndY);
      ctx.strokeStyle = "rgba(111, 85, 74, 0.45)";
      ctx.lineWidth = 1.2;
      ctx.stroke();
      ctx.textAlign = Math.cos(middle) >= 0 ? "left" : "right";
      ctx.fillStyle = "#6f554a";
      ctx.fillText(label, x, y);
      return;
    }

    ctx.textAlign = "center";
    ctx.fillStyle = "#4d3730";
    ctx.fillText(label, x, y);
  });
}

function renderAssetPie() {
  const rows = data.assetPie;
  const total = rows.reduce((sum, row) => sum + row.value, 0);
  drawPieChart(document.getElementById("assetPieChart"), rows);
  document.getElementById("assetPieLegend").innerHTML = rows
    .map((row) => {
      const pct = total ? ((row.value / total) * 100).toFixed(1) : "0.0";
      return `<div class="asset-pie-row">
        <span><i style="background:${row.color}"></i>${row.label} ${pct}%</span>
        <strong>${money.format(row.value)}</strong>
        <em>${pct}%</em>
      </div>`;
    })
    .join("");
}

function getPortfolioMetrics() {
  const tw = data.investments.tw;
  const us = data.investments.us;
  const taiwanStocks = assetValue("台股");
  const usStocks = assetValue("美股");
  const cash = assetValue("現金");
  const debt = assetValue("負債");
  const stockAssets = taiwanStocks + usStocks;
  const totalAssets = stockAssets + cash;
  const netWorth = totalAssets - debt;
  const monthlyRows = monthlyMetricRows();
  const latestMonth = monthlyRows[monthlyRows.length - 1] ?? monthlyFallback();
  const currentMonthFinance = currentFinanceMonth();
  const previousMonth = monthlyRows[monthlyRows.length - 2] ?? monthlyFallback();
  const latestIncome = Number(latestMonth.income) || 0;
  const latestExpense = Number(latestMonth.expense) || 0;
  const previousIncome = Number(previousMonth.income) || 0;
  const previousExpense = Number(previousMonth.expense) || 0;
  const latestNet = Number(latestMonth.net);
  const previousNet = Number(previousMonth.net);
  const monthNet = Number.isFinite(latestNet) ? latestNet : latestIncome - latestExpense;
  const previousMonthNet = Number.isFinite(previousNet) ? previousNet : previousIncome - previousExpense;
  const cashDays = Math.round((cash / Math.max(1, latestExpense)) * 30);
  const usMarketValue = us.holdings.reduce(
    (sum, holding) => sum + parseShares(holding.shares) * parseAmount(holding.price),
    0,
  );
  const usCost = us.holdings.reduce(
    (sum, holding) => sum + parseShares(holding.shares) * parseAmount(holding.cost),
    0,
  );
  const usGain = us.holdings.reduce((sum, holding) => sum + parseAmount(holding.gain), 0);
  const twShares = parseShares(tw.shares);
  const monthlyInvestment = investmentAmountForPeriod(currentMonthKey());
  const monthlyInvestmentRemaining = Math.max(0, MONTHLY_INVESTMENT_TARGET - Math.round(monthlyInvestment));
  const sinopacBalance = Number(data.accountBreakdown.sinopacBalance) || 0;
  const postOfficeBalance = Number(data.accountBreakdown.postOfficeBalance) || 0;
  const monthlySinopacTransfer = Number(currentMonthFinance?.sinopacTransfer) || 0;
  const investableSinopacCash = Math.max(0, Math.round(sinopacBalance - EMERGENCY_FUND_TARGET));
  const leveragedValue = data.rebalancer.leveragedValue || 0;
  const rebalanceTotal = leveragedValue + cash;
  const leveragedRatio = rebalanceTotal ? (leveragedValue / rebalanceTotal) * 100 : 0;
  const leveragedDrift = leveragedRatio - LEVERAGED_TARGET_RATIO;
  const twCostTwd = Number(tw.cost) || 0;
  const usCostTwd = us.costTwd ?? Math.round(usCost * usdToTwd);
  const usGainTwd = us.gainTwd ?? Math.round(usGain * usdToTwd);
  const investmentCostTwd = twCostTwd + Number(usCostTwd || 0);
  const investmentGainTwd = (Number(tw.gain) || 0) + Number(usGainTwd || 0);

  return {
    taiwanStocks,
    usStocks,
    cash,
    debt,
    stockAssets,
    totalAssets,
    netWorth,
    latestMonth,
    monthNet,
    monthNetChange: monthNet - previousMonthNet,
    cashDays,
    investmentRatio: percent(stockAssets, totalAssets),
    cashRatio: percent(cash, totalAssets),
    debtRatio: percent(debt, totalAssets),
    twPrice: twShares ? tw.marketValue / twShares : 0,
    twUnitCost: twShares ? tw.cost / twShares : 0,
    twCostTwd,
    twGainTwd: Number(tw.gain) || 0,
    usMarketValue,
    usCost,
    usGain,
    usMarketValueTwd: us.accountValueTwd ?? Math.round(usMarketValue * usdToTwd),
    usCostTwd,
    usGainTwd,
    usReturnRate: percent(usGain, usCost, 2),
    monthlyInvestment,
    monthlyInvestmentRemaining,
    sinopacBalance,
    postOfficeBalance,
    monthlySinopacTransfer,
    monthlySinopacTransferRemaining: Math.max(0, MONTHLY_INVESTMENT_TARGET - Math.round(monthlySinopacTransfer)),
    investableSinopacCash,
    investmentCostTwd,
    investmentGainTwd,
    investmentReturnRate: percent(investmentGainTwd, investmentCostTwd, 2),
    leveragedValue,
    rebalanceTotal,
    leveragedRatio,
    leveragedDrift,
    hasLeveragedHolding: data.rebalancer.hasLeveragedHolding,
  };
}

function getNextMilestone() {
  const { netWorth } = getPortfolioMetrics();
  const annualRunRate = estimateAnnualRunRate();
  const target = [5000000, 10000000, 15000000, 20000000].find((item) => item > netWorth) ?? 20000000;
  const progress = Math.min(100, Number(((netWorth / target) * 100).toFixed(1)));
  const remaining = Math.max(0, target - netWorth);
  const etaDate = estimateMilestoneDate(remaining, annualRunRate);
  const age = etaDate ? ageOnDate(etaDate) : null;
  return { target, progress, remaining, etaDate, age, annualRunRate };
}

function financeMonths() {
  return (window.financeData?.years ?? [])
    .flatMap((year) => year.months ?? [])
    .filter((month) => /^\d{4}-\d{2}$/.test(String(month.month || "")))
    .sort((a, b) => String(a.month).localeCompare(String(b.month)));
}

function estimateAnnualRunRate() {
  const months = financeMonths()
    .map((month) => Number(month.net) || 0)
    .filter((net) => Number.isFinite(net));
  if (!months.length) return 0;
  const recent = months.slice(-12);
  const positive = recent.filter((net) => net > 0);
  const basis = positive.length >= 3 ? positive : recent;
  const averageMonthly = basis.reduce((sum, net) => sum + net, 0) / Math.max(1, basis.length);
  return Math.max(0, averageMonthly * 12);
}

function estimateMilestoneDate(remaining, annualRunRate) {
  if (remaining <= 0) return new Date();
  if (!annualRunRate) return null;
  const monthsNeeded = Math.ceil(remaining / (annualRunRate / 12));
  if (!Number.isFinite(monthsNeeded) || monthsNeeded > 600) return null;
  const date = new Date();
  date.setMonth(date.getMonth() + monthsNeeded);
  return date;
}

function ageOnDate(date) {
  let age = date.getFullYear() - BIRTH_DATE.getFullYear();
  const beforeBirthday =
    date.getMonth() < BIRTH_DATE.getMonth()
    || (date.getMonth() === BIRTH_DATE.getMonth() && date.getDate() < BIRTH_DATE.getDate());
  return beforeBirthday ? age - 1 : age;
}

function formatEtaDate(date) {
  if (!date) return "持續追蹤中";
  return `${rocYear(date.getFullYear())} 年 ${date.getMonth() + 1} 月`;
}

function renderHero() {
  const metrics = getPortfolioMetrics();
  const next = getNextMilestone();
  const monthGrowth = percent(metrics.monthNet, Math.max(1, metrics.netWorth - metrics.monthNet), 2);
  document.getElementById("heroOverview").innerHTML = `
    <span>Total Net Worth</span>
    <strong>${money.format(metrics.netWorth)}</strong>
    <small class="positive">▲ ${money.format(metrics.monthNet)}（+${monthGrowth}）本月增長</small>`;

  document.getElementById("heroMilestone").innerHTML = `
    <div class="wealth-journey">
      <span class="wealth-journey-label">距離${number.format(next.target / 10000)}萬</span>
      <div class="wealth-journey-track" aria-label="財富旅程進度 ${next.progress}%">
        <span class="wealth-journey-fill" style="width:${next.progress}%"></span>
        <span class="wealth-journey-cat" style="left:${next.progress}%">
          <svg viewBox="0 0 32 32" role="img" aria-label="目前財富位置">
            <path class="journey-cat-face" d="M8 15c0-6 4-10 8-10s8 4 8 10c0 7-4 11-8 11s-8-4-8-11Z"></path>
            <path class="journey-cat-ear" d="M9 11 8 5l5 4"></path>
            <path class="journey-cat-ear" d="M23 11l1-6-5 4"></path>
            <path class="journey-cat-eye" d="M12 15c1 1 3 1 4 0"></path>
            <path class="journey-cat-eye" d="M17 15c1 1 3 1 4 0"></path>
            <path class="journey-cat-mouth" d="M14 20c1 1 3 1 4 0"></path>
          </svg>
        </span>
      </div>
      <span class="wealth-journey-goal" aria-hidden="true">🏆</span>
      <strong>${next.progress}%</strong>
    </div>
    <p>還差 ${money.format(next.remaining)} 到下一個財富里程碑</p>`;
}

function renderKpis() {
  const metrics = getPortfolioMetrics();
  const monthlyInvestmentRounded = Math.round(metrics.monthlyInvestment);
  const investmentGainTone = gainTone(metrics.investmentGainTwd);
  const rows = [
    { label: "總資產", value: money.format(metrics.totalAssets) },
    { label: "股票資產", value: money.format(metrics.stockAssets) },
    {
      label: "緊急預備金",
      value: money.format(metrics.cash),
    },
    { label: "負債", value: money.format(metrics.debt) },
    { label: "本月增加", value: money.format(metrics.monthNet) },
    {
      label: "本月投資",
      value: money.format(monthlyInvestmentRounded),
    },
    {
      label: `投資總損益(${compactPercent(metrics.investmentReturnRate)})`,
      value: money.format(metrics.investmentGainTwd),
      valueTone: investmentGainTone,
    },
  ];

  document.getElementById("kpiGrid").innerHTML = rows
    .map((row) => `<article class="kpi-card">
      <span>${row.label}</span>
      <strong class="${row.valueTone || ""}">${row.value}</strong>
      ${row.changeHtml ? `<small>${row.changeHtml}</small>` : ""}
      ${row.change ? `<small class="${row.tone}">${row.change}</small>` : ""}
      ${Number.isFinite(row.progress) ? `<span class="mini-progress"><i style="width:${row.progress}%"></i></span>` : ""}
      ${row.noteHtml ? `<em>${row.noteHtml}</em>` : ""}
      ${row.note ? `<em>${row.note}</em>` : ""}
    </article>`)
    .join("");
}

function healthTone(status) {
  if (status === "good") return "完成";
  if (status === "watch") return "留意";
  return "優先";
}

function rebalanceMessage(metrics) {
  if (!metrics.hasLeveragedHolding) {
    return {
      status: "watch",
      title: "00685L 資料",
      text: "首頁目前找不到 00685L，請先確認持股或更新股價。",
    };
  }
  if (!metrics.rebalanceTotal) {
    return {
      status: "watch",
      title: "再平衡比例",
      text: "目前可檢查金額為 0，更新資料後再判斷。",
    };
  }
  const drift = Math.abs(metrics.leveragedDrift);
  if (drift <= REBALANCE_BAND) {
    return {
      status: "good",
      title: "再平衡比例",
      text: `00685L 約 ${metrics.leveragedRatio.toFixed(1)}%，仍在 ${REBALANCE_BAND}% 容許範圍內。`,
    };
  }
  return {
    status: "warn",
    title: "再平衡比例",
    text: metrics.leveragedDrift > 0
      ? `00685L 約 ${metrics.leveragedRatio.toFixed(1)}%，比例偏高，下次投入先偏向現金。`
      : `00685L 約 ${metrics.leveragedRatio.toFixed(1)}%，比例偏低，下次投入可偏向 00685L。`,
  };
}

function nextContributionMessage(metrics) {
  if (metrics.monthlyInvestmentRemaining <= 0) return "本月投資目標已達成，下一筆投入可依 70/30 檢查比例微調。";
  if (!metrics.hasLeveragedHolding || !metrics.rebalanceTotal) {
    return `本月還可投入 ${money.format(metrics.monthlyInvestmentRemaining)}，資料完整後再判斷正2/現金比例。`;
  }
  if (metrics.leveragedRatio > LEVERAGED_TARGET_RATIO + REBALANCE_BAND) {
    return `本月還可投入 ${money.format(metrics.monthlyInvestmentRemaining)}，建議先留在現金，讓比例靠近 ${LEVERAGED_TARGET_RATIO}/${CASH_TARGET_RATIO}。`;
  }
  if (metrics.leveragedRatio < LEVERAGED_TARGET_RATIO - REBALANCE_BAND) {
    return `本月還可投入 ${money.format(metrics.monthlyInvestmentRemaining)}，下次可優先投入 00685L。`;
  }
  const leveragedAmount = Math.round(metrics.monthlyInvestmentRemaining * (LEVERAGED_TARGET_RATIO / 100));
  const cashAmount = metrics.monthlyInvestmentRemaining - leveragedAmount;
  return `本月還可投入 ${money.format(metrics.monthlyInvestmentRemaining)}，可參考 00685L ${money.format(leveragedAmount)} / 現金 ${money.format(cashAmount)}。`;
}

function monthlyInvestmentSummary(metrics) {
  const invested = Math.round(metrics.monthlyInvestment);
  const gap = MONTHLY_INVESTMENT_TARGET - invested;
  if (gap > 0) return `已投入 ${money.format(invested)}，還差 ${money.format(gap)} 達成本月目標。`;
  if (gap < 0) return `已投入 ${money.format(invested)}，本月已超過目標 ${money.format(Math.abs(gap))}。`;
  return `已投入 ${money.format(invested)}，剛好達成本月目標。`;
}

function rebalanceActionSummary(metrics) {
  if (!metrics.hasLeveragedHolding || !metrics.rebalanceTotal) return "資料完整後會自動判斷 00685L / 現金比例。";
  const ratioText = `00685L 約 ${metrics.leveragedRatio.toFixed(1)}%`;
  if (Math.abs(metrics.leveragedDrift) <= REBALANCE_BAND) {
    return `${ratioText}，比例接近目標，下一筆可照 ${LEVERAGED_TARGET_RATIO}/${CASH_TARGET_RATIO} 分配。`;
  }
  return metrics.leveragedDrift > 0
    ? `${ratioText}，比例偏高，下一筆先留現金。`
    : `${ratioText}，比例偏低，下一筆優先買 00685L。`;
}

function monthlyTransferSummary(metrics) {
  const transferred = Math.round(metrics.monthlySinopacTransfer);
  const gap = MONTHLY_INVESTMENT_TARGET - transferred;
  if (gap > 0) return `已轉入 ${money.format(transferred)}，還差 ${money.format(gap)} 到本月固定轉入目標。`;
  if (gap < 0) return `已轉入 ${money.format(transferred)}，本月已超過固定轉入目標 ${money.format(Math.abs(gap))}。`;
  return `已轉入 ${money.format(transferred)}，本月固定轉入已完成。`;
}

function investableCashSummary(metrics) {
  if (!metrics.sinopacBalance) return "請先在更新資產快照填永豐餘額，才會知道今天能不能加碼。";
  if (metrics.sinopacBalance < EMERGENCY_FUND_TARGET) {
    return `永豐 ${money.format(metrics.sinopacBalance)}，低於緊急預備金 ${money.format(EMERGENCY_FUND_TARGET)}，先不要加碼。`;
  }
  if (metrics.investableSinopacCash <= 0) {
    return `永豐剛好達緊急預備金 ${money.format(EMERGENCY_FUND_TARGET)}，今天先不要動用。`;
  }
  return `可加碼 ${money.format(metrics.investableSinopacCash)}，永豐 ${money.format(metrics.sinopacBalance)} - 緊急預備金 ${money.format(EMERGENCY_FUND_TARGET)}。`;
}

function todayConclusion(metrics) {
  if (!metrics.sinopacBalance) {
    return {
      status: "warn",
      text: "先填永豐餘額，今天能不能加碼才會準。",
    };
  }
  if (metrics.sinopacBalance < EMERGENCY_FUND_TARGET) {
    return {
      status: "warn",
      text: `先不要買，永豐低於緊急預備金 ${money.format(EMERGENCY_FUND_TARGET)}。`,
    };
  }
  if (metrics.monthlySinopacTransferRemaining > 0) {
    return {
      status: "watch",
      text: `先轉 ${money.format(metrics.monthlySinopacTransferRemaining)} 到永豐，再看買點。`,
    };
  }
  if (metrics.investableSinopacCash <= 0) {
    return {
      status: "warn",
      text: "永豐剛好達緊急預備金，今天先不要加碼。",
    };
  }
  if (metrics.monthlyInvestmentRemaining <= 0) {
    return {
      status: "good",
      text: "本月已達標，今天不用急著買。",
    };
  }
  return {
    status: "good",
    text: `可加碼 ${money.format(Math.min(metrics.investableSinopacCash, metrics.monthlyInvestmentRemaining))}。`,
  };
}

function nextActionSummary(metrics) {
  if (!metrics.sinopacBalance) return "先補上永豐餘額，下一筆建議才會準。";
  if (metrics.investableSinopacCash <= 0) return "先守住永豐緊急預備金，不建議買進。";
  if (metrics.monthlySinopacTransferRemaining > 0) {
    return `本月還差 ${money.format(metrics.monthlySinopacTransferRemaining)} 轉入永豐，先完成轉入再看買點。`;
  }
  if (metrics.monthlyInvestmentRemaining <= 0) return "本月投資已達標，下一筆不用急，照 70/30 檢查比例。";
  return nextContributionMessage(metrics);
}

function availableContributionBudget(metrics) {
  return Math.max(0, Math.min(
    Number(metrics.investableSinopacCash) || 0,
    Number(metrics.monthlyInvestmentRemaining) || 0,
  ));
}

function leveragedPriceSignalText(metrics) {
  const signal = data.leveragedPullbackSignal || { state: "idle" };
  if (!metrics.hasLeveragedHolding) return "目前找不到 00685L 持股，請先確認交易紀錄或更新股價。";
  if (signal.state === "idle" || signal.state === "loading") return "正在讀取 00685L 近 20 個交易日價格。";
  if (signal.state === "error") return "00685L 歷史價格暫時無法讀取，先用本月投資額度與 70/30 比例判斷。";
  const base = `近 20 日高點 ${stockPriceTwd.format(signal.high)}，現價 ${stockPriceTwd.format(signal.price)}，回落 ${signal.pullback.toFixed(1)}%。`;
  if (!metrics.sinopacBalance || metrics.investableSinopacCash <= 0) {
    return `${base} 分級：禁止加碼，先守住永豐緊急預備金。`;
  }
  if (metrics.monthlyInvestmentRemaining <= 0) {
    return `${base} 分級：只觀察，本月投資目標已達標。`;
  }
  const budget = availableContributionBudget(metrics);
  if (signal.pullback < 5) return `${base} 分級：一般投入，照本月 ${money.format(MONTHLY_INVESTMENT_TARGET)} 計畫，不額外加碼。`;
  if (signal.pullback < 10) return `${base} 分級：觀察，先不用急著加碼，可保留本月剩餘額度 ${money.format(budget)}。`;
  if (signal.pullback < 15) {
    return `${base} 分級：小額加碼，這次最多 ${money.format(Math.min(10000, budget))}。`;
  }
  return `${base} 分級：分批加碼，本月最多用 ${money.format(budget)}，分 2-3 筆，每筆最多 ${money.format(Math.min(10000, budget))}。`;
}

function leveragedPriceSignalStatus() {
  const signal = data.leveragedPullbackSignal || { state: "idle" };
  if (signal.state !== "ready") return "watch";
  if (signal.pullback >= 15) return "warn";
  if (signal.pullback >= 10) return "watch";
  return "good";
}

function loadLeveragedPullbackSignal() {
  const signal = data.leveragedPullbackSignal || {};
  const today = todayKey();
  if (signal.state === "loading" || signal.checkedKey === today) return;
  data.leveragedPullbackSignal = { state: "loading", checkedKey: today };
  const url = `https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date=${today}&stockNo=00685L&response=json`;
  fetchWithTimeout(url, { cache: "no-store" }, 8000)
    .then((response) => {
      if (!response.ok) throw new Error("TWSE history unavailable");
      return response.json();
    })
    .then((payload) => {
      const rows = (payload.data || [])
        .map((row) => ({
          date: row[0],
          high: parseAmount(row[4]),
          price: parseAmount(row[6]),
        }))
        .filter((row) => Number.isFinite(row.high) && row.high > 0 && Number.isFinite(row.price) && row.price > 0)
        .slice(-20);
      if (!rows.length) throw new Error("TWSE history empty");
      const latest = rows[rows.length - 1];
      const high = Math.max(...rows.map((row) => row.high));
      data.leveragedPullbackSignal = {
        state: "ready",
        checkedKey: today,
        price: latest.price,
        high,
        pullback: high ? ((high - latest.price) / high) * 100 : 0,
        priceDate: latest.date,
      };
      renderTodayActions();
    })
    .catch(() => {
      data.leveragedPullbackSignal = { state: "error", checkedKey: today };
      renderTodayActions();
    });
}

function savingsRateStatus(rate) {
  if (rate >= 50) return { className: "good", label: "很好" };
  if (rate >= 30) return { className: "watch", label: "穩定" };
  return { className: "warn", label: "留意" };
}

function currentFinanceMonth() {
  const monthKey = currentMonthKey();
  if (data.currentMonthFinance?.month === monthKey) return data.currentMonthFinance;
  return financeMonths().find((month) => month.month === monthKey) || null;
}

function renderTodayActions() {
  const target = document.getElementById("todayActions");
  if (!target) return;
  const metrics = getPortfolioMetrics();
  if (metrics.hasLeveragedHolding) loadLeveragedPullbackSignal();
  const conclusion = todayConclusion(metrics);
  const rows = [
    {
      status: conclusion.status,
      title: "今日結論",
      text: conclusion.text,
    },
    {
      status: metrics.monthlySinopacTransferRemaining <= 0 ? "good" : "watch",
      title: "本月轉入永豐",
      text: monthlyTransferSummary(metrics),
    },
    {
      status: metrics.sinopacBalance && metrics.investableSinopacCash > 0 ? "good" : "warn",
      title: "永豐可用現金",
      text: investableCashSummary(metrics),
    },
    { status: leveragedPriceSignalStatus(), title: "加碼資金分級", text: leveragedPriceSignalText(metrics) },
    {
      status: metrics.monthlyInvestmentRemaining <= 0 ? "good" : "watch",
      title: "本月投資進度",
      text: monthlyInvestmentSummary(metrics),
    },
    { status: "watch", title: "下一筆建議", text: nextActionSummary(metrics) },
  ];

  target.innerHTML = rows
    .map((item) => `<div class="today-action ${item.status}">
      <span>${healthTone(item.status)}</span>
      <div>
        <strong>${item.title}</strong>
        <p>${item.text}</p>
      </div>
    </div>`)
    .join("");
}

function formatStatusDate(value) {
  if (!value) return "尚未記錄";
  return formatUpdateTime(value);
}

function dataStatusMessage(status) {
  if (!status) return "讀取中";
  if (status.fallbackActive) return "Supabase 失敗，已自動改用 SQLite";
  return status.dataStatus || "讀取中";
}

function currentDbNote(status) {
  if (!status) return "";
  if (status.fallbackActive) return "SQLite Fallback active";
  if (status.currentDb === "supabase") return "Connected";
  return status.supabaseConfigured ? "SQLite fallback ready" : "Local SQLite";
}

function dataStatusNote(status) {
  if (!status) return "";
  if (status.fallbackActive) return "SQLite fallback active";
  if (status.currentDb === "supabase") return "Supabase Connected";
  if (status.dataStatus === "範例資料") return "請先匯入正式備份";
  return "SQLite data";
}

function databaseHealth(status) {
  if (!status) return { value: "讀取中", note: "" };
  if (status.fallbackActive) return { value: "Fallback", note: "SQLite Online" };
  return { value: "Online", note: status.currentDb === "supabase" ? "Supabase Connected" : "SQLite Online" };
}

function renderDataStatusCards() {
  const status = dataStatus;
  updateMigrateButtonVisibility(status);
  const target = document.getElementById("dataStatusCards");
  if (!target || target.hidden) return;
  const metadata = status?.metadata ?? {};
  const health = databaseHealth(status);
  const rows = [
    { label: "Current DB", value: status?.currentDbLabel ?? "讀取中", note: currentDbNote(status) },
    { label: "Database Health", value: health.value, note: health.note },
    { label: "Last Price Update", value: formatStatusDate(metadata.lastPriceUpdate || data.updatedAt), note: "" },
    { label: "Data Status", value: dataStatusMessage(status), note: dataStatusNote(status) },
  ];
  target.innerHTML = rows
    .map((row) => `<article class="data-status-card">
      <span>${row.label}</span>
      <strong>${row.value}</strong>
      ${row.note ? `<small>${row.note}</small>` : ""}
    </article>`)
    .join("");
}

function updateMigrateButtonVisibility(status) {
  const button = document.getElementById("migrateSupabaseButton");
  if (!button) return;
  button.hidden = true;
}

function renderInitialLoading() {
  renderPriceUpdateNotice("正在載入正式資料...");
  const ledger = document.getElementById("yearAccordion");
  if (ledger) ledger.innerHTML = '<div class="loading-row">正在載入年度/月度對帳...</div>';
}

let chartState = { points: [], rows: [] };

function drawNetWorthChart() {
  const canvas = document.getElementById("netWorthChart");
  const { ctx, width, height } = fitCanvas(canvas);
  const rows = data.assetTrend.filter((row) => row.month >= "2025-01");
  if (!rows.length) {
    rows.push({ month: "Demo", assets: getPortfolioMetrics().netWorth });
  }
  const area = { left: 58, top: 24, width: width - 78, height: height - 70 };
  const values = rows.map((row) => row.assets);
  const max = Math.max(...values);
  const min = Math.min(...values);
  const span = Math.max(1, max - min);
  ctx.clearRect(0, 0, width, height);
  drawGrid(ctx, area, 4);

  const points = rows.map((row, index) => ({
    x: area.left + (area.width / Math.max(1, rows.length - 1)) * index,
    y: area.top + area.height - ((row.assets - min) / span) * area.height,
    row,
  }));

  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, area.top + area.height);
    ctx.lineTo(point.x, point.y);
  });
  ctx.lineTo(points[points.length - 1].x, area.top + area.height);
  ctx.closePath();
  const fill = ctx.createLinearGradient(0, area.top, 0, area.top + area.height);
  fill.addColorStop(0, "rgba(233, 163, 173, 0.28)");
  fill.addColorStop(1, "rgba(233, 163, 173, 0)");
  ctx.fillStyle = fill;
  ctx.fill();

  ctx.strokeStyle = "#e9a3ad";
  ctx.lineWidth = 3;
  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y);
    else ctx.lineTo(point.x, point.y);
  });
  ctx.stroke();

  ctx.fillStyle = "#b8acd3";
  points.forEach((point) => {
    ctx.beginPath();
    ctx.arc(point.x, point.y, 3.6, 0, Math.PI * 2);
    ctx.fill();
  });

  ctx.fillStyle = "#8b7a72";
  ctx.font = "12px system-ui";
  ctx.textAlign = "center";
  rows.forEach((row, index) => {
    if (index % 4 === 0 || index === rows.length - 1) {
      ctx.fillText(row.month.slice(0, 4), points[index].x, height - 24);
    }
  });

  chartState = { points, rows };
}

function setupChartHover() {
  const canvas = document.getElementById("netWorthChart");
  const tooltip = document.getElementById("chartTooltip");
  if (canvas.dataset.hoverReady) return;
  canvas.dataset.hoverReady = "true";
  canvas.addEventListener("mousemove", (event) => {
    if (!chartState.points.length) return;
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const nearest = chartState.points.reduce((best, point) =>
      Math.abs(point.x - x) < Math.abs(best.x - x) ? point : best,
    );
    tooltip.style.opacity = "1";
    tooltip.style.left = `${Math.min(rect.width - 130, Math.max(8, nearest.x - 48))}px`;
    tooltip.style.top = `${Math.max(8, nearest.y - 58)}px`;
    tooltip.innerHTML = `<span>${formatMonthLabel(nearest.row.month)}</span><strong>${money.format(nearest.row.assets)}</strong>`;
  });
  canvas.addEventListener("mouseleave", () => {
    tooltip.style.opacity = "0";
  });
}

function renderMilestones() {
  const { netWorth } = getPortfolioMetrics();
  const annualRunRate = estimateAnnualRunRate();
  const targets = [5000000, 10000000, 15000000, 20000000];
  document.getElementById("milestones").innerHTML = targets
    .map((target) => {
      const progress = Math.min(100, Math.round((netWorth / target) * 100));
      const remaining = Math.max(0, target - netWorth);
      const etaDate = estimateMilestoneDate(remaining, annualRunRate);
      const age = etaDate ? ageOnDate(etaDate) : null;
      const etaText = age === null ? "持續追蹤中" : `${formatEtaDate(etaDate)}（${age}歲）`;
      return `<div class="milestone-row">
        <div>
          <strong>${number.format(target / 10000)}萬</strong>
          <span>預估 ${etaText}</span>
        </div>
        <em>${progress}%</em>
        <span class="bar-track"><i style="width:${progress}%"></i></span>
      </div>`;
    })
    .join("");
}

function renderDataUpdates() {
  const fx = data.fxNote.match(/=\s*([\d.]+)/)?.[1] ?? "31.451";
  usdToTwd = Number(fx) || usdToTwd;
  setSidebarUpdatedAt(data.updatedAt);
  const warning = localStorage.getItem("wealthDashboardUpdateWarning");
  if (warning) {
    renderUpdateWarning(warning === "1" ? undefined : warning);
    return;
  }
  const rows = [
    { label: "資料更新", value: formatUpdateTime(data.updatedAt) },
    { label: "台股更新", value: formatUpdateTime(data.investments.tw.updatedAt) },
    { label: "美股更新", value: formatUpdateTime(data.investments.us.updatedAt) },
  ];
  document.getElementById("dataUpdates").innerHTML = rows
    .map((row) => `<div class="update-row"><span>${row.label}</span><strong>${row.value}</strong></div>`)
    .join("");
}

function renderUpdateWarning(message = "部分資料更新失敗，請查看更新結果") {
  document.getElementById("dataUpdates").innerHTML = `
    <div class="update-row"><span>提醒</span><strong>${escapeHtml(message)}</strong></div>
  `;
}

function priceUpdateMessages(payload) {
  const messages = [];
  if (payload.failedSymbols?.length) messages.push(`暫時保留原價：${payload.failedSymbols.join(", ")}`);
  if (payload.errorMessages?.length) messages.push(...payload.errorMessages);
  return [...new Set(messages.filter(Boolean))];
}

function marketUpdateLabel(updatedAt, fallbackAt, failedSymbols = []) {
  if (updatedAt || fallbackAt) return failedSymbols.length ? "部分保留原價" : "已更新";
  return "尚未更新";
}

function renderUpdateResult(payload = {}) {
  const marketUpdates = payload.marketUpdates ?? {};
  setSidebarUpdatedAt(payload.updatedAt || data.updatedAt);
  const failedSymbols = payload.failedSymbols || [];
  const rows = [
    { label: "台股", value: marketUpdateLabel(marketUpdates.TW, data.investments.tw.updatedAt, failedSymbols.filter((symbol) => /^\d/.test(symbol))) },
    { label: "美股", value: marketUpdateLabel(marketUpdates.US, data.investments.us.updatedAt, failedSymbols.filter((symbol) => !/^\d/.test(symbol))) },
    { label: "匯率", value: payload.fxRate ? `1 USD = ${payload.fxRate} TWD` : data.fxNote.replace("美股以 ", "") },
    { label: "最近更新", value: formatUpdateTime(payload.updatedAt || data.updatedAt) },
  ];
  document.getElementById("dataUpdates").innerHTML = rows
    .map((row) => `<div class="update-row"><span>${row.label}</span><strong>${row.value}</strong></div>`)
    .join("");
}

function renderDetailedPriceUpdate(payload) {
  renderUpdateResult(payload);
  const messages = priceUpdateMessages(payload);
  if (!messages.length) return;
  const rows = [
    ...Array.from(document.getElementById("dataUpdates").querySelectorAll(".update-row")).map((row) => ({
      label: row.querySelector("span")?.textContent || "",
      value: row.querySelector("strong")?.textContent || "",
    })),
    ...messages.map((message) => ({ label: "提醒", value: message })),
  ];
  document.getElementById("dataUpdates").innerHTML = rows
    .map((row) => `<div class="update-row"><span>${escapeHtml(row.label)}</span><strong>${escapeHtml(row.value)}</strong></div>`)
    .join("");
}

function renderPriceUpdateNotice(message) {
  document.getElementById("dataUpdates").innerHTML = `
    <div class="update-row"><span>股價狀態</span><strong>${escapeHtml(message)}</strong></div>
  `;
}

function renderPriceUpdateProgress(stage, detail = "", elapsedSeconds = 0) {
  const rows = [
    { label: "股價更新", value: stage },
    { label: "進度", value: detail },
    { label: "已等待", value: `${elapsedSeconds} 秒` },
  ];
  document.getElementById("dataUpdates").innerHTML = rows
    .map((row) => `<div class="update-row"><span>${escapeHtml(row.label)}</span><strong>${escapeHtml(row.value)}</strong></div>`)
    .join("");
}

function startPriceProgress(button, automatic = false) {
  const startedAt = Date.now();
  const stages = [
    { after: 0, button: automatic ? "自動更新中" : "連線中...", stage: "連線到股價服務", detail: "準備更新台股與美股" },
    { after: 6, button: "取得台股...", stage: "取得台股價格", detail: "0050" },
    { after: 14, button: "取得美股...", stage: "取得美股價格", detail: "GOOG, TSM, MU, VOO, NVDA" },
    { after: 26, button: "寫入資料...", stage: "寫入 Supabase", detail: "保存最新價格與狀態" },
    { after: 42, button: "快完成了...", stage: "等待資料來源回應", detail: "若來源較慢會保留前次價格" },
  ];

  const paint = () => {
    const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    const current = stages.reduce((selected, stage) => (elapsed >= stage.after ? stage : selected), stages[0]);
    if (button) button.textContent = current.button;
    renderPriceUpdateProgress(current.stage, current.detail, elapsed);
  };
  paint();
  const timer = setInterval(paint, 4000);
  return {
    startedAt,
    stop: () => clearInterval(timer),
  };
}

function markTodayPriceUpdated() {
  if (!window.localStorage) return;
  window.localStorage.setItem(AUTO_PRICE_UPDATE_KEY, todayKey());
}

function setupPriceUpdater() {
  const button = document.getElementById("updatePricesButton");
  if (!button || button.dataset.ready) return;
  button.dataset.ready = "true";
  button.addEventListener("click", async () => {
    if (priceUpdateInProgress) return;
    const originalText = button.textContent;
    let progress = null;
    priceUpdateInProgress = true;
    button.disabled = true;
    progress = startPriceProgress(button);
    try {
      const response = await fetchWithTimeout("/api/update-prices", { method: "POST", cache: "no-store" }, PRICE_UPDATE_TIMEOUT_MS);
      if (handleAuthExpired(response)) return;
      const payload = await readApiPayload(response, "股價更新失敗，請重新整理或確認登入狀態。");
      if (!response.ok) {
        const missingRouteMessage =
          response.status === 404
            ? "找不到股價更新 API，請重新啟動資產管理服務後再試一次。"
            : "";
        throw new Error(payload.detail || missingRouteMessage || `股價更新失敗（HTTP ${response.status}）`);
      }
      localStorage.removeItem("wealthDashboardUpdateWarning");
      markTodayPriceUpdated();
      renderPriceUpdateProgress("更新完成", "重新整理首頁資料", Math.floor((Date.now() - progress.startedAt) / 1000));
      await loadExternalData();
      await fetchWithTimeout("/api/prices", { cache: "no-store" }, 12000);
      render();
      renderDetailedPriceUpdate(payload);
      button.textContent = payload.warnings?.length ? "部分更新完成" : "股價更新完成";
      if (payload.warnings?.length) {
        console.warn("股價更新警告", payload.warnings);
        const warningMessage = priceUpdateMessages(payload).join("｜") || "部分資料更新失敗，請查看更新結果";
        if (payload.source === "demo") {
          button.textContent = "範例資料";
          renderPriceUpdateNotice(warningMessage);
        } else {
          localStorage.setItem("wealthDashboardUpdateWarning", warningMessage);
          renderDetailedPriceUpdate(payload);
        }
      }
      button.disabled = false;
      setTimeout(() => {
        button.textContent = originalText;
      }, 1800);
    } catch (error) {
      button.textContent = "更新失敗";
      console.warn("股價更新失敗", error);
      const message = error.name === "AbortError" || String(error.message || "").includes("Request timeout")
        ? "股價更新等候過久，已先保留原本資料。"
        : error.message || "股價更新失敗";
      localStorage.setItem("wealthDashboardUpdateWarning", message);
      renderUpdateWarning(message);
      setTimeout(() => {
        button.textContent = originalText;
        button.disabled = false;
      }, 1600);
    } finally {
      if (progress) progress.stop();
      priceUpdateInProgress = false;
    }
  });
}

async function runAutomaticPriceUpdate() {
  if (priceUpdateInProgress) return;
  priceUpdateInProgress = true;
  const progress = startPriceProgress(null, true);
  try {
    const response = await fetchWithTimeout("/api/update-prices", { method: "POST", cache: "no-store" }, PRICE_UPDATE_TIMEOUT_MS);
    if (handleAuthExpired(response)) return;
    const payload = await readApiPayload(response);
    if (!response.ok) throw new Error(payload.detail || `自動更新失敗（HTTP ${response.status}）`);
    markTodayPriceUpdated();
    localStorage.removeItem("wealthDashboardUpdateWarning");
    renderPriceUpdateProgress("自動更新完成", "重新整理首頁資料", Math.floor((Date.now() - progress.startedAt) / 1000));
    await loadExternalData();
    await fetchWithTimeout("/api/prices", { cache: "no-store" }, 12000);
    render();
    if (payload.warnings?.length) {
      console.warn("自動更新股價警告", payload.warnings);
      renderDetailedPriceUpdate(payload);
      return;
    }
    renderDetailedPriceUpdate(payload);
  } catch (error) {
    console.warn("自動更新股價失敗", error);
    renderDataUpdates();
  } finally {
    progress.stop();
    priceUpdateInProgress = false;
  }
}

function setupAutomaticPriceRefresh() {
  if (priceAutoRefreshTimer || window.location.protocol === "file:") return;
  priceAutoRefreshTimer = window.setInterval(() => {
    if (!document.hidden) runAutomaticPriceUpdate();
  }, PRICE_AUTO_REFRESH_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) runAutomaticPriceUpdate();
  });
}

async function readApiPayload(response, fallbackMessage = "資料讀取失敗，請重新整理或確認登入狀態。") {
  const text = await response.text();
  try {
    return text ? JSON.parse(text) : {};
  } catch {
    return { detail: fallbackMessage };
  }
}

function renderInvestmentCards() {
  const { tw, us } = data.investments;
  const metrics = getPortfolioMetrics();
  const twGainTone = gainTone(tw.gain);
  const usGainTone = gainTone(metrics.usGainTwd);
  const twHoldings = tw.holdings?.length
    ? tw.holdings
    : [{
        title: tw.title,
        shares: tw.shares,
        price: metrics.twPrice,
        cost: metrics.twUnitCost,
        gain: tw.gain,
        returnRate: tw.returnRate,
      }];
  document.getElementById("twInvestment").innerHTML = `
    <div class="investment-hero">
      <span>市值</span>
      <strong>${money.format(tw.marketValue)}</strong>
    </div>
    <div class="investment-stats">
      <div><span>市值</span><strong>${money.format(tw.marketValue)}</strong></div>
      <div><span>成本</span><strong>${money.format(tw.cost)}</strong></div>
      <div><span>損益</span><strong class="${twGainTone}">${money.format(tw.gain)}</strong></div>
      <div><span>報酬率</span><strong class="${twGainTone}">${tw.returnRate}</strong></div>
    </div>
    <div class="holding-list stock-table">
      <div class="holding-row holding-head">
        <span>股票名稱</span>
        <span>股數</span>
        <span>現價</span>
        <span>成本</span>
        <span>損益</span>
        <span>報酬率</span>
      </div>
      ${twHoldings
        .map((holding) => {
          const tone = gainTone(holding.gain);
          return `<div class="holding-row">
          <strong>${holding.title}</strong>
          <span>${holding.shares}</span>
          <span>${Number(holding.price).toFixed(2)}</span>
          <span>${Number(holding.cost).toFixed(2)}</span>
          <span class="${tone}">${money.format(holding.gain)}</span>
          <span class="${tone}">${holding.returnRate}</span>
        </div>`;
        })
        .join("")}
    </div>
    <p class="note-text">更新時間：${formatUpdateTime(tw.updatedAt)}</p>`;

  document.getElementById("usInvestment").innerHTML = `
    <div class="investment-hero">
      <span>市值</span>
      <strong>${formatUsdDisplay(metrics.usMarketValue)}</strong>
    </div>
    <div class="investment-stats">
      <div><span>市值</span><strong>${formatUsdSummary(metrics.usMarketValue)}</strong><small>${formatTwdApprox(metrics.usMarketValueTwd)}</small></div>
      <div><span>成本</span><strong>${formatUsdSummary(metrics.usCost)}</strong><small>${formatTwdApprox(metrics.usCostTwd)}</small></div>
      <div><span>損益</span><strong class="${usGainTone}">${formatUsdSummary(metrics.usGain)}</strong><small class="${usGainTone}">${formatTwdApprox(metrics.usGainTwd)}</small></div>
      <div><span>報酬率</span><strong class="${usGainTone}">${metrics.usReturnRate}</strong></div>
    </div>
    <div class="holding-list stock-table us-holdings">
      <div class="holding-row holding-head">
        <span>股票名稱</span>
        <span>股數</span>
        <span>現價</span>
        <span>成本</span>
        <span>損益</span>
        <span>報酬率</span>
      </div>
      ${us.holdings
        .map((holding) => {
          const tone = gainTone(holding.gainTwd);
          return `<div class="holding-row">
          <strong>${holding.symbol}</strong>
          <span>${holding.shares}</span>
          <span>${holding.price}<small>${formatPlainTwdApproxFromUsd(holding.priceValue)}</small></span>
          <span>${holding.cost}<small>${formatPlainTwdApproxFromUsd(holding.costValue)}</small></span>
          <span class="${tone}">${holding.gain}<small>${formatPlainTwdApproxFromUsd(holding.gainValue, true)}</small></span>
          <span class="${tone}">${holding.returnRate}</span>
        </div>`;
        })
        .join("")}
    </div>
    <p class="note-text">更新時間：${formatUpdateTime(us.updatedAt ?? "2026/05/30 05:10")}</p>`;
}

function renderRankList(targetId, rows, valueKey = "value", labelKey = "label") {
  const max = Math.max(...rows.map((row) => Math.abs(row[valueKey])));
  document.getElementById(targetId).innerHTML = rows
    .map((row, index) => {
      const pct = max ? Math.max(2, (Math.abs(row[valueKey]) / max) * 100) : 0;
      return `<div class="rank-row">
        <span>${row[labelKey]}</span>
        <strong>${money.format(row[valueKey])}</strong>
        <span class="bar-track"><i style="width:${pct}%; background:${colors[index % colors.length]}"></i></span>
      </div>`;
    })
    .join("");
}

function formatMonthLabel(month) {
  const [year, mm] = month.split("-");
  return `${rocYear(year)} 年 ${Number(mm)} 月`;
}

function renderYearSummary(year) {
  const yearlySavings = savingsRateStatus(Number(year.savingsRate) || 0);
  return [
    { label: "全年收入", value: money.format(year.income), tone: "income-positive" },
    { label: "全年支出", value: money.format(year.expense), tone: "expense-negative" },
    { label: "全年淨增加", value: money.format(year.net), tone: year.net >= 0 ? "positive" : "negative" },
    {
      label: "全年儲蓄率",
      value: `${year.savingsRate}%`,
      tone: year.savingsRate >= 0 ? "positive" : "negative",
      badge: yearlySavings,
    },
  ]
    .map((item) => `<div>
      <span>${item.label}</span>
      <strong class="${item.tone}">${item.value}</strong>
      ${item.badge ? `<em class="savings-badge ${item.badge.className}">${item.badge.label}</em>` : ""}
    </div>`)
    .join("");
}

function renderMonthlyRows(year) {
  return (year?.months ?? [])
    .slice()
    .sort((a, b) => a.month.localeCompare(b.month))
    .map((month) => {
      const savings = savingsRateStatus(Number(month.savingsRate) || 0);
      return `<tr>
      <td>${formatMonthLabel(month.month)}</td>
      <td class="income-positive">${money.format(month.income)}</td>
      <td class="expense-negative">${money.format(month.expense)}</td>
      <td class="${month.net >= 0 ? "positive" : "negative"}">${money.format(month.net)}</td>
      <td class="${month.savingsRate >= 0 ? "positive" : "negative"}">${month.savingsRate}% <em class="savings-badge ${savings.className}">${savings.label}</em></td>
    </tr>`;
    })
    .join("");
}

function renderLedger() {
  const target = document.getElementById("yearAccordion");
  if (!target) return;
  if (!window.financeData?.years?.length) {
    target.innerHTML = '<div class="empty-state">目前沒有可顯示的年度/月度對帳資料。</div>';
    return;
  }
  const years = applyDividendIncomeToFinanceYears(window.financeData.years ?? []);
  if (!years.length) {
    target.innerHTML = '<div class="empty-state">目前沒有可顯示的年度/月度對帳資料。</div>';
    return;
  }
  target.innerHTML = years
    .map((year, index) => `<details class="year-detail" ${index === 0 ? "open" : ""}>
      <summary>${rocYear(year.year)} 年</summary>
      <div class="year-summary">${renderYearSummary(year)}</div>
      <div class="monthly-table-wrap">
        <table class="monthly-table">
          <thead>
            <tr>
              <th>月份</th>
              <th>收入</th>
              <th>支出</th>
              <th>淨增加</th>
              <th>儲蓄率</th>
            </tr>
          </thead>
          <tbody>${renderMonthlyRows(year)}</tbody>
        </table>
      </div>
    </details>`)
    .join("");
}

function render() {
  renderHero();
  renderKpis();
  renderTodayActions();
  renderAssetPie();
  drawNetWorthChart();
  setupChartHover();
  renderMilestones();
  renderDataUpdates();
  renderDataStatusCards();
  renderInvestmentCards();
  renderLedger();
}

async function refreshDataStatus() {
  try {
    const response = await fetchWithTimeout("/api/db/status", { cache: "no-store" });
    if (handleAuthExpired(response)) return;
    if (!response.ok) return;
    dataStatus = await response.json();
  } catch {
    dataStatus = null;
  }
}

async function loadExternalData() {
  if (window.portfolioData) {
    applyPortfolioData(window.portfolioData, window.netWorthHistory ?? []);
  }

  async function fetchJson(primaryPath, examplePath, fallbackValue, payloadKey = "") {
    const isLiveApi = window.location.protocol !== "file:" && primaryPath.startsWith("/api/");
    const attempts = isLiveApi ? [22000, 30000] : [10000];
    for (const timeoutMs of attempts) {
      try {
        const primaryResponse = await fetchWithTimeout(primaryPath, { cache: "no-store" }, timeoutMs);
        if (handleAuthExpired(primaryResponse)) return fallbackValue;
        if (primaryResponse.ok) {
          const payload = await primaryResponse.json();
          return payloadKey ? payload[payloadKey] : payload;
        }
      } catch {
        // Retry live API once before deciding whether to use fallback data.
      }
    }

    if (isLiveApi) return fallbackValue;

    if (examplePath) {
      try {
        const exampleResponse = await fetchWithTimeout(examplePath, { cache: "no-store" });
        if (exampleResponse.ok) return exampleResponse.json();
      } catch {
        // Use fallback value below.
      }
    }

    return fallbackValue;
  }

  refreshDataStatus()
    .then(renderDataStatusCards)
    .catch(() => {});

  let core = await fetchJson("/api/dashboard-core", "", null);
  if (core?.portfolio) {
    rememberDashboardCore(core);
  } else {
    core = readLastDashboardCore();
    if (core?.portfolio) {
      renderPriceUpdateNotice("目前連線不穩，先顯示上次成功載入的資料。");
    }
  }
  let transactionsLoaded = false;
  let dividendsLoaded = false;
  if (core?.portfolio) {
    applyPortfolioData(core.portfolio, core.history || []);
    applyAccountData(core.accounts || {});
    applyCurrentMonthFinance(core.currentMonthFinance || null);
    render();
    if (core.transactions) {
      applyTransactionData(core.transactions);
      transactionsLoaded = true;
      render();
    }
    if (core.dividends) {
      applyDividendData(core.dividends);
      dividendsLoaded = true;
      render();
    }
  } else {
    const [portfolio, history] = await Promise.all([
      fetchJson("/api/portfolio", "./data/example-portfolio.json", null, "portfolio"),
      fetchJson("/api/net-worth-history", "./data/example-net-worth-history.json", [], "history"),
    ]);
    if (portfolio) {
      applyPortfolioData(portfolio, history);
      render();
    }
  }

  const financeDataPromise = fetchJson("/api/finance-data", "", null, "financeData");

  if (!transactionsLoaded) {
    const transactions = await fetchJson("/api/transactions", "./data/example-transactions.json", [], "transactions");
    if (transactions) {
      applyTransactionData(transactions);
      render();
    }
  }

  if (!dividendsLoaded) {
    const dividends = await fetchJson("/api/dividends", "./data/example-dividends.json", [], "dividends");
    if (dividends) {
      applyDividendData(dividends);
      render();
    }
  }

  const financeData = await financeDataPromise;
  if (financeData?.years?.length) window.financeData = financeData;
  render();
}

async function runDailyDataHealthCheck() {
  try {
    await fetchWithTimeout("/api/data-health", { cache: "no-store" }, 12000);
  } catch {
    // Settings page shows detailed health status; the dashboard should stay quiet.
  }
}

window.addEventListener("resize", render);
setupPriceUpdater();

async function initializeDashboard() {
  if (window.location.protocol === "file:") {
    renderPriceUpdateNotice("請用啟動檔開啟新版網站：http://127.0.0.1:8000/");
    return;
  }
  renderInitialLoading();
  try {
    await loadExternalData();
    render();
  } catch (error) {
    console.warn("首頁資料載入失敗", error);
    renderPriceUpdateNotice("資料載入不穩，請重新整理或稍後再試。");
    render();
  }
  window.setTimeout(runDailyDataHealthCheck, 45000);
  setupAutomaticPriceRefresh();
  await runAutomaticPriceUpdate();
}

initializeDashboard();
