const form = document.getElementById("transactionForm");
const rows = document.getElementById("transactionRows");
const statusText = document.getElementById("statusText");
const submitButton = document.getElementById("submitButton");
const cancelEditButton = document.getElementById("cancelEditButton");
const modeBanner = document.getElementById("modeBanner");
const filters = {
  search: document.getElementById("searchFilter"),
  market: document.getElementById("marketFilter"),
  action: document.getElementById("actionFilter"),
  year: document.getElementById("yearFilter"),
  month: document.getElementById("monthFilter"),
};

let transactions = [];
let editingId = null;
let lastAutoFilledName = "";
const stockNameMap = new Map(Object.entries({
  "0050": "元大台灣50",
  GOOG: "Alphabet",
  MU: "Micron Technology",
  NVDA: "NVIDIA",
  TSM: "Taiwan Semiconductor",
  VOO: "Vanguard S&P 500 ETF",
}));

form.elements.date.value = formatInputRocDate(new Date());

function formatNumber(value) {
  return new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 6 }).format(Number(value) || 0);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function rocYear(year) {
  return Number(year) - 1911;
}

function formatInputRocDate(date) {
  const year = rocYear(date.getFullYear());
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}/${month}/${day}`;
}

function normalizeInputDate(value) {
  const text = String(value || "").trim();
  const parts = text.replaceAll(".", "/").replaceAll("-", "/").split("/").map((part) => part.trim()).filter(Boolean);
  if (parts.length !== 3) throw new Error("日期格式請輸入民國年，例如 115/06/06");
  let [year, month, day] = parts.map((part) => Number(part));
  if (!year || !month || !day) throw new Error("日期格式請輸入民國年，例如 115/06/06");
  if (year < 1911) year += 1911;
  const date = new Date(year, month - 1, day);
  const valid = date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day;
  if (!valid) throw new Error("日期不是有效日期");
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function formatDisplayDate(value) {
  const date = new Date(`${value || ""}T00:00:00`);
  if (Number.isNaN(date.getTime())) return String(value || "");
  return formatInputRocDate(date);
}

function normalizeSymbol(value) {
  return String(value || "").trim().toUpperCase();
}

function rememberStockName(symbol, name) {
  const normalizedSymbol = normalizeSymbol(symbol);
  const normalizedName = String(name || "").trim();
  if (normalizedSymbol && normalizedName) stockNameMap.set(normalizedSymbol, normalizedName);
}

function learnStockNames(items) {
  (items || []).forEach((item) => rememberStockName(item.symbol, item.name));
}

async function loadPortfolioStockNames() {
  try {
    const response = await fetch("/api/portfolio", { cache: "no-store" });
    if (!response.ok) return;
    const payload = await response.json();
    learnStockNames(payload.portfolio?.holdings || []);
  } catch {
    // Existing transaction names are enough if the portfolio is temporarily unavailable.
  }
}

function autoFillStockName() {
  const symbol = normalizeSymbol(form.elements.symbol.value);
  form.elements.symbol.value = symbol;
  const name = stockNameMap.get(symbol);
  if (!name) return;
  const currentName = String(form.elements.name.value || "").trim();
  if (currentName && currentName !== lastAutoFilledName) return;
  form.elements.name.value = name;
  lastAutoFilledName = name;
}

function setTransactions(nextTransactions) {
  transactions = nextTransactions || [];
  learnStockNames(transactions);
  populateDateFilters();
  renderTransactions();
}

function setStatus(message, tone = "") {
  statusText.textContent = message;
  statusText.className = `status${tone ? ` ${tone}` : ""}`;
}

function setFormMode(id = null) {
  editingId = id;
  submitButton.textContent = editingId ? "更新交易" : "新增交易";
  cancelEditButton.hidden = !editingId;
  if (modeBanner) {
    const transaction = transactions.find((item) => item.id === editingId);
    modeBanner.classList.toggle("editing", Boolean(editingId));
    modeBanner.innerHTML = editingId && transaction
      ? `<span><strong>編輯模式</strong> 正在修改 ${escapeHtml(formatDisplayDate(transaction.date))} ${escapeHtml(transaction.market)}:${escapeHtml(transaction.symbol)}，完成後請按「更新交易」。</span>`
      : `<span><strong>新增模式</strong> 填寫下方欄位後儲存一筆新交易。</span>`;
  }
  renderTransactions();
}

function resetForm() {
  form.reset();
  form.elements.date.value = formatInputRocDate(new Date());
  form.elements.fee.value = "0";
  lastAutoFilledName = "";
  setFormMode(null);
}

function transactionTime(item) {
  const time = new Date(`${item.date || ""}T00:00:00`).getTime();
  return Number.isNaN(time) ? 0 : time;
}

function sortedTransactions(items) {
  return items.slice().sort((a, b) => transactionTime(b) - transactionTime(a));
}

function activeFilters() {
  return {
    search: filters.search.value.trim().toLowerCase(),
    market: filters.market.value,
    action: filters.action.value,
    year: filters.year.value,
    month: filters.month.value,
  };
}

function transactionMatches(item, filter) {
  const date = String(item.date || "");
  const searchText = [item.symbol, item.name, item.note].join(" ").toLowerCase();
  return (
    (!filter.search || searchText.includes(filter.search)) &&
    (!filter.market || item.market === filter.market) &&
    (!filter.action || item.action === filter.action) &&
    (!filter.year || date.slice(0, 4) === filter.year) &&
    (!filter.month || date.slice(5, 7) === filter.month)
  );
}

function filteredTransactions() {
  const filter = activeFilters();
  return sortedTransactions(transactions.filter((item) => transactionMatches(item, filter)));
}

function populateDateFilters() {
  const selectedYear = filters.year.value;
  const selectedMonth = filters.month.value;
  const years = [...new Set(transactions.map((item) => String(item.date || "").slice(0, 4)).filter(Boolean))].sort((a, b) => b.localeCompare(a));
  const months = [...new Set(transactions.map((item) => String(item.date || "").slice(5, 7)).filter(Boolean))].sort();

  filters.year.innerHTML = `<option value="">全部</option>${years.map((year) => `<option value="${escapeHtml(year)}">${escapeHtml(rocYear(year))} 年</option>`).join("")}`;
  filters.month.innerHTML = `<option value="">全部</option>${months.map((month) => `<option value="${escapeHtml(month)}">${escapeHtml(month)}</option>`).join("")}`;
  filters.year.value = years.includes(selectedYear) ? selectedYear : "";
  filters.month.value = months.includes(selectedMonth) ? selectedMonth : "";
}

function renderTransactions() {
  const visibleTransactions = filteredTransactions();
  if (!visibleTransactions.length) {
    rows.innerHTML = `<tr><td colspan="9">尚無交易紀錄</td></tr>`;
    return;
  }

  rows.innerHTML = visibleTransactions
    .map(
      (item) => `
        <tr class="${item.id === editingId ? "editing-row" : ""}">
          <td>${escapeHtml(formatDisplayDate(item.date))}</td>
          <td><span class="badge">${escapeHtml(item.market)}</span></td>
          <td>${escapeHtml(item.symbol)}<br />${escapeHtml(item.name)}</td>
          <td><span class="badge">${escapeHtml(item.action)}</span></td>
          <td>${formatNumber(item.shares)}</td>
          <td>${formatNumber(item.price)}</td>
          <td>${formatNumber(item.fee)}</td>
          <td>${escapeHtml(item.note)}</td>
          <td>
            <div class="row-actions">
              <button class="secondary" type="button" data-action="edit" data-id="${escapeHtml(item.id)}">編輯</button>
              <button class="danger" type="button" data-action="delete" data-id="${escapeHtml(item.id)}">刪除</button>
            </div>
          </td>
        </tr>
      `,
    )
    .join("");
}

function currentTransaction() {
  const formData = new FormData(form);
  const symbol = normalizeSymbol(formData.get("symbol"));
  const name = String(formData.get("name")).trim();
  rememberStockName(symbol, name);
  return {
    date: normalizeInputDate(formData.get("date")),
    market: formData.get("market"),
    symbol,
    name,
    action: formData.get("action"),
    shares: Number(formData.get("shares")),
    price: Number(formData.get("price")),
    fee: Number(formData.get("fee") || 0),
    note: String(formData.get("note") || "").trim(),
  };
}

function editTransaction(id) {
  const transaction = transactions.find((item) => item.id === id);
  if (!transaction) return;

  form.elements.date.value = formatDisplayDate(transaction.date);
  form.elements.market.value = transaction.market || "TW";
  form.elements.symbol.value = transaction.symbol || "";
  form.elements.name.value = transaction.name || "";
  lastAutoFilledName = "";
  form.elements.action.value = transaction.action || "BUY";
  form.elements.shares.value = transaction.shares ?? "";
  form.elements.price.value = transaction.price ?? "";
  form.elements.fee.value = transaction.fee ?? 0;
  form.elements.note.value = transaction.note || "";
  setFormMode(id);
  setStatus("正在編輯交易，確認欄位後按「更新交易」。", "working");
  form.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadTransactions() {
  await loadPortfolioStockNames();
  const response = await fetch("/api/transactions", { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "交易紀錄載入失敗");
  setTransactions(payload.transactions || []);
  setStatus(`已載入 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`, "success");
}

async function saveTransaction() {
  const url = editingId ? `/api/transactions/${encodeURIComponent(editingId)}` : "/api/transactions";
  const method = editingId ? "PUT" : "POST";
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(currentTransaction()),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "交易儲存失敗");
  return payload;
}

async function deleteTransaction(id) {
  const transaction = transactions.find((item) => item.id === id);
  if (!transaction) return;
  const label = `${formatDisplayDate(transaction.date)} ${transaction.market}:${transaction.symbol} ${transaction.action}`;
  if (!window.confirm(`確定刪除這筆交易？\n${label}`)) return;

  setStatus("刪除中...", "working");
  const response = await fetch(`/api/transactions/${encodeURIComponent(id)}`, { method: "DELETE" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "交易刪除失敗");

  setTransactions(payload.transactions || []);
  if (editingId === id) resetForm();
  setStatus(`已刪除，現在共有 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`, "success");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submitButton.disabled = true;
  setStatus(editingId ? "更新中..." : "儲存中...", "working");

  try {
    const payload = await saveTransaction();
    setTransactions(payload.transactions || []);
    setStatus(
      editingId
        ? `已更新，現在共有 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`
        : `已新增，現在共有 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`,
      "success",
    );
    resetForm();
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    submitButton.disabled = false;
  }
});

cancelEditButton.addEventListener("click", () => {
  resetForm();
  setStatus(`已載入 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`, "success");
});

function applyFilters() {
  renderTransactions();
  setStatus(`已載入 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`, "success");
}

Object.values(filters).forEach((input) => {
  input.addEventListener("input", applyFilters);
  input.addEventListener("change", applyFilters);
});

form.elements.symbol.addEventListener("input", autoFillStockName);
form.elements.symbol.addEventListener("change", autoFillStockName);
form.elements.symbol.addEventListener("blur", autoFillStockName);
form.elements.market.addEventListener("change", autoFillStockName);
form.elements.name.addEventListener("input", () => {
  if (form.elements.name.value.trim() !== lastAutoFilledName) lastAutoFilledName = "";
});

rows.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action][data-id]");
  if (!button) return;

  const { action, id } = button.dataset;
  if (action === "edit") {
    editTransaction(id);
    return;
  }

  if (action === "delete") {
    button.disabled = true;
    try {
      await deleteTransaction(id);
    } catch (error) {
      setStatus(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }
});

loadTransactions().catch((error) => {
  setStatus(error.message, "error");
  renderTransactions();
});
