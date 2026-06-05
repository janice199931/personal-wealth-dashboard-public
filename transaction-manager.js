const form = document.getElementById("transactionForm");
const rows = document.getElementById("transactionRows");
const statusText = document.getElementById("statusText");
const submitButton = document.getElementById("submitButton");
const cancelEditButton = document.getElementById("cancelEditButton");
const filters = {
  search: document.getElementById("searchFilter"),
  market: document.getElementById("marketFilter"),
  action: document.getElementById("actionFilter"),
  year: document.getElementById("yearFilter"),
  month: document.getElementById("monthFilter"),
};

let transactions = [];
let editingId = null;

form.elements.date.valueAsDate = new Date();

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

function setTransactions(nextTransactions) {
  transactions = nextTransactions || [];
  populateDateFilters();
  renderTransactions();
}

function setFormMode(id = null) {
  editingId = id;
  submitButton.textContent = editingId ? "更新交易" : "新增交易";
  cancelEditButton.hidden = !editingId;
}

function resetForm() {
  form.reset();
  form.elements.date.valueAsDate = new Date();
  form.elements.fee.value = "0";
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

  filters.year.innerHTML = `<option value="">全部</option>${years.map((year) => `<option value="${escapeHtml(year)}">${escapeHtml(year)}</option>`).join("")}`;
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
        <tr>
          <td>${escapeHtml(item.date)}</td>
          <td>${escapeHtml(item.market)}</td>
          <td>${escapeHtml(item.symbol)}<br />${escapeHtml(item.name)}</td>
          <td>${escapeHtml(item.action)}</td>
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
  return {
    date: formData.get("date"),
    market: formData.get("market"),
    symbol: String(formData.get("symbol")).trim().toUpperCase(),
    name: String(formData.get("name")).trim(),
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

  form.elements.date.value = transaction.date || "";
  form.elements.market.value = transaction.market || "TW";
  form.elements.symbol.value = transaction.symbol || "";
  form.elements.name.value = transaction.name || "";
  form.elements.action.value = transaction.action || "BUY";
  form.elements.shares.value = transaction.shares ?? "";
  form.elements.price.value = transaction.price ?? "";
  form.elements.fee.value = transaction.fee ?? 0;
  form.elements.note.value = transaction.note || "";
  setFormMode(id);
  statusText.textContent = "正在編輯交易";
  form.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadTransactions() {
  const response = await fetch("/api/transactions", { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "交易紀錄載入失敗");
  setTransactions(payload.transactions || []);
  statusText.textContent = `已載入 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`;
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
  const label = `${transaction.date} ${transaction.market}:${transaction.symbol} ${transaction.action}`;
  if (!window.confirm(`確定刪除這筆交易？\n${label}`)) return;

  statusText.textContent = "刪除中...";
  const response = await fetch(`/api/transactions/${encodeURIComponent(id)}`, { method: "DELETE" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "交易刪除失敗");

  setTransactions(payload.transactions || []);
  if (editingId === id) resetForm();
  statusText.textContent = `已刪除，現在共有 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submitButton.disabled = true;
  statusText.textContent = editingId ? "更新中..." : "儲存中...";

  try {
    const payload = await saveTransaction();
    setTransactions(payload.transactions || []);
    statusText.textContent = editingId
      ? `已更新，現在共有 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`
      : `已新增，現在共有 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`;
    resetForm();
  } catch (error) {
    statusText.textContent = error.message;
  } finally {
    submitButton.disabled = false;
  }
});

cancelEditButton.addEventListener("click", () => {
  resetForm();
  statusText.textContent = `已載入 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`;
});

function applyFilters() {
  renderTransactions();
  statusText.textContent = `已載入 ${transactions.length} 筆交易，顯示 ${filteredTransactions().length} 筆`;
}

Object.values(filters).forEach((input) => {
  input.addEventListener("input", applyFilters);
  input.addEventListener("change", applyFilters);
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
      statusText.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }
});

loadTransactions().catch((error) => {
  statusText.textContent = error.message;
  renderTransactions();
});
