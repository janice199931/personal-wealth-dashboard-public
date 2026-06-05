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
};

const colors = ["#e9a3ad", "#f0bd76", "#a8c4a0", "#b8acd3", "#9ec7dc", "#d7a7ad"];
const money = new Intl.NumberFormat("zh-TW", {
  style: "currency",
  currency: "TWD",
  maximumFractionDigits: 0,
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
const BIRTH_DATE = new Date("1999-08-31T00:00:00+08:00");
let dataStatus = null;

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

function formatTaiwanDate(dateText) {
  const [year, month, day] = dateText.split("/").map((part) => Number(part));
  if (!year || !month || !day) return data.investments.tw.updatedAt;
  return `${year + 1911}/${String(month).padStart(2, "0")}/${String(day).padStart(2, "0")}`;
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
  const normalized = text.includes("T") ? text : text.replaceAll("/", "-");
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return text;
  const parts = new Intl.DateTimeFormat("zh-TW", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date).reduce((acc, part) => ({ ...acc, [part.type]: part.value }), {});
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
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
  return data.monthly.length ? data.monthly : [monthlyFallback(), monthlyFallback()];
}

function percent(value, total, digits = 1) {
  return total ? `${((value / total) * 100).toFixed(digits)}%` : "0.0%";
}

function signedMoney(value, formatter = money) {
  const formatted = formatter.format(Math.abs(value));
  return `${value >= 0 ? "+" : "-"}${formatted}`;
}

function signedPercent(value) {
  return `${value >= 0 ? "+" : ""}${Number(value).toFixed(1)}%`;
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

function dividendNetTwd(dividend) {
  const amount = Number(dividend.amount) || 0;
  const tax = Number(dividend.tax) || 0;
  const net = amount - tax;
  return dividend.currency === "USD" ? net * usdToTwd : net;
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
  const twHoldings = (portfolio.holdings ?? []).filter((holding) => holding.market === "TW");
  const usHoldings = (portfolio.holdings ?? []).filter((holding) => holding.market === "US");

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
      title: `${holding.name} ${holding.symbol}`,
      shares: formatSharesValue(holding.shares),
      price: Number(holding.price),
      cost: Number(holding.averageCost),
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

  if (history.length) {
    data.assetTrend = history.map((row) => ({
      month: row.date,
      assets: row.netWorth,
    }));
  }
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
  const previousMonth = monthlyRows[monthlyRows.length - 2] ?? monthlyFallback();
  const latestIncome = Number(latestMonth.income) || 0;
  const latestExpense = Number(latestMonth.expense) || 0;
  const previousIncome = Number(previousMonth.income) || 0;
  const previousExpense = Number(previousMonth.expense) || 0;
  const monthNet = latestIncome - latestExpense;
  const previousMonthNet = previousIncome - previousExpense;
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
    usMarketValue,
    usCost,
    usGain,
    usMarketValueTwd: us.accountValueTwd ?? Math.round(usMarketValue * usdToTwd),
    usCostTwd: us.costTwd ?? Math.round(usCost * usdToTwd),
    usGainTwd: us.gainTwd ?? Math.round(usGain * usdToTwd),
    usReturnRate: percent(usGain, usCost, 2),
  };
}

function getNextMilestone() {
  const { netWorth } = getPortfolioMetrics();
  const annualRunRate = estimateAnnualRunRate();
  const target = [5000000, 10000000, 15000000, 20000000].find((item) => item > netWorth) ?? 20000000;
  const progress = Math.min(100, Math.round((netWorth / target) * 100));
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
  return `${date.getFullYear()} 年 ${date.getMonth() + 1} 月`;
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
    <div class="hero-milestone-line">
      <span>距離 ${number.format(next.target / 10000)} 萬</span>
      <div class="hero-progress">
        <span style="width:${next.progress}%"></span>
      </div>
      <strong>${next.progress}%</strong>
      <em>${next.age === null ? "預估持續追蹤中" : `預估 ${next.age} 歲達成`}</em>
    </div>
    <p>還差 ${money.format(next.remaining)} 到下一個財富里程碑</p>`;
}

function renderKpis() {
  const metrics = getPortfolioMetrics();
  const rows = [
    { label: "總資產", value: money.format(metrics.totalAssets) },
    { label: "股票資產", value: money.format(metrics.stockAssets) },
    { label: "現金", value: money.format(metrics.cash) },
    { label: "負債", value: money.format(metrics.debt) },
    { label: "本月增加", value: money.format(metrics.monthNet) },
  ];

  document.getElementById("kpiGrid").innerHTML = rows
    .map((row) => `<article class="kpi-card">
      <span>${row.label}</span>
      <strong>${row.value}</strong>
      ${row.change ? `<small class="${row.tone}">${row.change}</small>` : ""}
    </article>`)
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

function backupReminder(status) {
  const lastBackup = status?.metadata?.lastBackup;
  if (!lastBackup) return "尚未匯出備份，建議先下載一份。";
  const backupTime = new Date(lastBackup).getTime();
  if (!Number.isFinite(backupTime)) return "";
  const days = Math.floor((Date.now() - backupTime) / 86400000);
  return days >= 7 ? `已 ${days} 天未備份，建議今天匯出。` : "";
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
    { label: "Last Backup", value: formatStatusDate(metadata.lastBackup), note: backupReminder(status) },
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
  button.hidden = status?.currentDb === "supabase" && !status?.fallbackActive;
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
    <div class="update-row"><span>Warning</span><strong>${escapeHtml(message)}</strong></div>
  `;
}

function priceUpdateMessages(payload) {
  const messages = [];
  if (payload.currentDb) messages.push(`Current DB: ${payload.currentDb}`);
  if (payload.savedTo) messages.push(`Saved to: ${payload.savedTo}`);
  if (payload.updatedSymbols?.length) messages.push(`Updated: ${payload.updatedSymbols.join(", ")}`);
  if (payload.failedSymbols?.length) messages.push(`Failed: ${payload.failedSymbols.join(", ")}`);
  if (payload.errorMessages?.length) messages.push(...payload.errorMessages);
  if (payload.twResult?.batchError) messages.push(`TW batch: ${payload.twResult.batchError}`);
  return [...new Set(messages.filter(Boolean))];
}

function renderUpdateResult(payload) {
  const marketUpdates = payload.marketUpdates ?? {};
  setSidebarUpdatedAt(payload.updatedAt || data.updatedAt);
  const rows = [
    { label: "股價更新完成", value: "" },
    { label: "資料更新", value: formatUpdateTime(payload.updatedAt || data.updatedAt) },
    { label: "台股更新", value: formatUpdateTime(marketUpdates.TW || data.investments.tw.updatedAt) },
    { label: "美股更新", value: formatUpdateTime(marketUpdates.US || data.investments.us.updatedAt) },
  ];
  document.getElementById("dataUpdates").innerHTML = rows
    .map((row) => `<div class="update-row"><span>${row.label}</span><strong>${row.value}</strong></div>`)
    .join("");
}

function renderDetailedPriceUpdate(payload) {
  const marketUpdates = payload.marketUpdates ?? {};
  setSidebarUpdatedAt(payload.updatedAt || data.updatedAt);
  const rows = [
    { label: "股價更新", value: payload.failedSymbols?.length ? "部分完成" : "完成" },
    { label: "Current DB", value: payload.currentDb || payload.source || "" },
    { label: "Saved To", value: payload.savedTo || "" },
    { label: "Updated", value: (payload.updatedSymbols || []).join(", ") || "無" },
    { label: "Failed", value: (payload.failedSymbols || []).join(", ") || "無" },
    { label: "資料更新", value: formatUpdateTime(payload.updatedAt || data.updatedAt) },
    { label: "台股更新", value: formatUpdateTime(marketUpdates.TW || data.investments.tw.updatedAt) },
    { label: "美股更新", value: formatUpdateTime(marketUpdates.US || data.investments.us.updatedAt) },
    ...priceUpdateMessages(payload).map((message) => ({ label: "Detail", value: message })),
  ];
  document.getElementById("dataUpdates").innerHTML = rows
    .map((row) => `<div class="update-row"><span>${escapeHtml(row.label)}</span><strong>${escapeHtml(row.value)}</strong></div>`)
    .join("");
}

function renderPriceUpdateNotice(message) {
  document.getElementById("dataUpdates").innerHTML = `
    <div class="update-row"><span>Auto Price</span><strong>${escapeHtml(message)}</strong></div>
  `;
}

function markTodayPriceUpdated() {
  if (!window.localStorage) return;
  window.localStorage.setItem(AUTO_PRICE_UPDATE_KEY, todayKey());
}

function hasAutoUpdatedToday() {
  if (!window.localStorage) return false;
  return window.localStorage.getItem(AUTO_PRICE_UPDATE_KEY) === todayKey();
}

function setupPriceUpdater() {
  const button = document.getElementById("updatePricesButton");
  if (!button || button.dataset.ready) return;
  button.dataset.ready = "true";
  button.addEventListener("click", async () => {
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "更新中...";
    try {
      const response = await fetch("/api/update-prices", { method: "POST" });
      const responseText = await response.text();
      let payload = {};
      try {
        payload = responseText ? JSON.parse(responseText) : {};
      } catch (error) {
        payload = { detail: responseText || "伺服器回傳格式錯誤" };
      }
      if (!response.ok) {
        const missingRouteMessage =
          response.status === 404
            ? "找不到股價更新 API，請重新啟動資產管理服務後再試一次。"
            : "";
        throw new Error(payload.detail || missingRouteMessage || `股價更新失敗（HTTP ${response.status}）`);
      }
      localStorage.removeItem("wealthDashboardUpdateWarning");
      markTodayPriceUpdated();
      await loadExternalData();
      await fetch("/api/prices", { cache: "no-store" });
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
    } catch (error) {
      button.textContent = "更新失敗";
      console.warn("股價更新失敗", error);
      localStorage.setItem("wealthDashboardUpdateWarning", error.message || "股價更新失敗");
      renderUpdateWarning(error.message || "股價更新失敗");
      setTimeout(() => {
        button.textContent = originalText;
        button.disabled = false;
      }, 1600);
    }
  });
}

async function runAutomaticPriceUpdate() {
  if (hasAutoUpdatedToday()) return;
  renderPriceUpdateNotice("正在自動更新股價…");
  try {
    const response = await fetch("/api/update-prices", { method: "POST" });
    const payload = await readApiPayload(response);
    if (!response.ok) throw new Error(payload.detail || `自動更新失敗（HTTP ${response.status}）`);
    markTodayPriceUpdated();
    localStorage.removeItem("wealthDashboardUpdateWarning");
    await loadExternalData();
    await fetch("/api/prices", { cache: "no-store" });
    render();
    if (payload.warnings?.length) {
      console.warn("自動更新股價警告", payload.warnings);
      renderDetailedPriceUpdate(payload);
      return;
    }
    renderDetailedPriceUpdate(payload);
  } catch (error) {
    console.warn("自動更新股價失敗", error);
    renderPriceUpdateNotice("自動更新失敗，可手動按更新股價");
  }
}

async function readApiPayload(response) {
  const text = await response.text();
  try {
    return text ? JSON.parse(text) : {};
  } catch {
    return { detail: text || "伺服器回傳格式錯誤" };
  }
}

function setBackupStatus(message) {
  const target = document.getElementById("backupStatus");
  if (target) target.textContent = message;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function setupDataBackupControls() {
  const exportButton = document.getElementById("exportBackupButton");
  const importButton = document.getElementById("importBackupButton");
  const rebuildButton = document.getElementById("rebuildPortfolioButton");
  const migrateButton = document.getElementById("migrateSupabaseButton");
  const fileInput = document.getElementById("backupFileInput");
  if (!exportButton || !importButton || !rebuildButton || !fileInput || exportButton.dataset.ready) return;
  exportButton.dataset.ready = "true";

  exportButton.addEventListener("click", async () => {
    exportButton.disabled = true;
    setBackupStatus("正在匯出備份...");
    try {
      const response = await fetch("/api/db/export-json", { cache: "no-store" });
      if (!response.ok) {
        const payload = await readApiPayload(response);
        throw new Error(payload.detail || `匯出失敗（HTTP ${response.status}）`);
      }
      const blob = await response.blob();
      const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
      downloadBlob(blob, `wealth-dashboard-backup-${stamp}.json`);
      setBackupStatus("備份已下載到這台電腦。");
      await refreshDataStatus();
      renderDataStatusCards();
    } catch (error) {
      console.warn("匯出備份失敗", error);
      setBackupStatus(error.message || "匯出備份失敗。");
    } finally {
      exportButton.disabled = false;
    }
  });

  importButton.addEventListener("click", () => {
    fileInput.value = "";
    fileInput.click();
  });

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    if (!window.confirm("匯入前會檢查備份完整性；完整備份會覆蓋正式資料。確定匯入？")) return;
    importButton.disabled = true;
    setBackupStatus("正在匯入備份...");
    try {
      const formData = new FormData();
      formData.append("backup", file);
      const response = await fetch("/api/db/import-json", { method: "POST", body: formData });
      const payload = await readApiPayload(response);
      if (!response.ok) throw new Error(payload.detail || `匯入失敗（HTTP ${response.status}）`);
      setBackupStatus("備份已匯入，正在重新整理...");
      setTimeout(() => window.location.reload(), 900);
    } catch (error) {
      console.warn("匯入備份失敗", error);
      setBackupStatus(error.message || "匯入備份失敗。");
    } finally {
      importButton.disabled = false;
    }
  });

  rebuildButton.addEventListener("click", async () => {
    rebuildButton.disabled = true;
    setBackupStatus("正在重建投資組合...");
    try {
      const response = await fetch("/api/db/rebuild-portfolio", { method: "POST" });
      const payload = await readApiPayload(response);
      if (!response.ok) throw new Error(payload.detail || `重建失敗（HTTP ${response.status}）`);
      setBackupStatus("投資組合已重建，正在重新整理...");
      setTimeout(() => window.location.reload(), 900);
    } catch (error) {
      console.warn("重建投資組合失敗", error);
      setBackupStatus(error.message || "重建投資組合失敗。");
    } finally {
      rebuildButton.disabled = false;
    }
  });

  if (migrateButton) {
    migrateButton.addEventListener("click", async () => {
      migrateButton.disabled = true;
      setBackupStatus("正在遷移到 Supabase...");
      try {
        const response = await fetch("/api/db/migrate-to-supabase", { method: "POST" });
        const payload = await readApiPayload(response);
        if (!response.ok) throw new Error(payload.detail || `遷移失敗（HTTP ${response.status}）`);
        setBackupStatus("Supabase 遷移完成，正在重新整理...");
        setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        console.warn("Supabase 遷移失敗", error);
        setBackupStatus(error.message || "Supabase 遷移失敗。");
      } finally {
        migrateButton.disabled = false;
      }
    });
  }
}

function renderInvestmentCards() {
  const { tw, us } = data.investments;
  const metrics = getPortfolioMetrics();
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
      <div><span>損益</span><strong class="positive">${money.format(tw.gain)}</strong></div>
      <div><span>報酬率</span><strong class="positive">${tw.returnRate}</strong></div>
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
        .map((holding) => `<div class="holding-row">
          <strong>${holding.title}</strong>
          <span>${holding.shares}</span>
          <span>${Number(holding.price).toFixed(2)}</span>
          <span>${Number(holding.cost).toFixed(2)}</span>
          <span class="positive">${money.format(holding.gain)}</span>
          <span class="positive">${holding.returnRate}</span>
        </div>`)
        .join("")}
    </div>
    <p class="note-text">更新時間：${cleanUpdateTime(tw.updatedAt)}</p>`;

  document.getElementById("usInvestment").innerHTML = `
    <div class="investment-hero">
      <span>市值</span>
      <strong>${formatUsdDisplay(metrics.usMarketValue)}</strong>
    </div>
    <div class="investment-stats">
      <div><span>市值</span><strong>${formatUsdSummary(metrics.usMarketValue)}</strong><small>${formatTwdApprox(metrics.usMarketValueTwd)}</small></div>
      <div><span>成本</span><strong>${formatUsdSummary(metrics.usCost)}</strong><small>${formatTwdApprox(metrics.usCostTwd)}</small></div>
      <div><span>損益</span><strong class="positive">${formatUsdSummary(metrics.usGain)}</strong><small>${formatTwdApprox(metrics.usGainTwd)}</small></div>
      <div><span>報酬率</span><strong class="positive">${metrics.usReturnRate}</strong></div>
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
        .map((holding) => `<div class="holding-row">
          <strong>${holding.symbol}</strong>
          <span>${holding.shares}</span>
          <span>${holding.price}<small>${formatPlainTwdApproxFromUsd(holding.priceValue)}</small></span>
          <span>${holding.cost}<small>${formatPlainTwdApproxFromUsd(holding.costValue)}</small></span>
          <span class="positive">${holding.gain}<small>${formatPlainTwdApproxFromUsd(holding.gainValue, true)}</small></span>
          <span class="positive">${holding.returnRate}</span>
        </div>`)
        .join("")}
    </div>
    <p class="note-text">更新時間：${cleanUpdateTime(us.updatedAt ?? "2026/05/30 05:10")}</p>`;
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
  return `${year} 年 ${Number(mm)} 月`;
}

function renderYearSummary(year) {
  return [
    { label: "全年收入", value: money.format(year.income), tone: "positive" },
    ...(year.investmentIncome ? [{ label: "投資收入", value: money.format(year.investmentIncome), tone: "positive" }] : []),
    { label: "全年支出", value: money.format(year.expense), tone: "negative" },
    { label: "全年淨增加", value: money.format(year.net), tone: year.net >= 0 ? "positive" : "negative" },
    { label: "全年儲蓄率", value: `${year.savingsRate}%`, tone: year.savingsRate >= 0 ? "positive" : "negative" },
  ]
    .map((item) => `<div>
      <span>${item.label}</span>
      <strong class="${item.tone}">${item.value}</strong>
    </div>`)
    .join("");
}

function renderMonthlyRows(year) {
  return (year?.months ?? [])
    .slice()
    .sort((a, b) => a.month.localeCompare(b.month))
    .map((month) => `<tr>
      <td>${formatMonthLabel(month.month)}</td>
      <td class="positive">${money.format(month.income)}</td>
      <td class="negative">${money.format(month.expense)}</td>
      <td class="${month.net >= 0 ? "positive" : "negative"}">${money.format(month.net)}</td>
      <td class="${month.savingsRate >= 0 ? "positive" : "negative"}">${month.savingsRate}%</td>
    </tr>`)
    .join("");
}

function renderLedger() {
  if (!window.financeData) return;
  const years = applyDividendIncomeToFinanceYears(window.financeData.years ?? []);
  document.getElementById("yearAccordion").innerHTML = years
    .map((year, index) => `<details class="year-detail" ${index === 0 ? "open" : ""}>
      <summary>${year.year}</summary>
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
    try {
      const primaryResponse = await fetchWithTimeout(primaryPath, { cache: "no-store" });
      if (primaryResponse.ok) {
        const payload = await primaryResponse.json();
        return payloadKey ? payload[payloadKey] : payload;
      }
    } catch {
      // Fall through to example data or fallback value.
    }

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

  const financeData = await fetchJson("/api/finance-data", "", null, "financeData");
  const portfolio = await fetchJson("/api/portfolio", "./data/example-portfolio.json", null, "portfolio");
  const history = await fetchJson("/api/net-worth-history", "./data/example-net-worth-history.json", [], "history");
  const dividends = await fetchJson("/api/dividends", "./data/example-dividends.json", [], "dividends");
  await refreshDataStatus().catch(() => {});

  if (portfolio) {
    applyPortfolioData(portfolio, history);
  }
  if (dividends) {
    applyDividendData(dividends);
  }
  if (financeData?.years?.length) window.financeData = financeData;
}

window.addEventListener("resize", render);
setupPriceUpdater();
setupDataBackupControls();

async function initializeDashboard() {
  if (window.location.protocol === "file:") {
    renderPriceUpdateNotice("請用啟動檔開啟新版網站：http://127.0.0.1:8000/");
    return;
  }
  renderInitialLoading();
  await loadExternalData();
  render();
  await runAutomaticPriceUpdate();
}

initializeDashboard();
