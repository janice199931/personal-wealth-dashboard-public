# Personal Wealth Dashboard Public Edition

這是可上傳 GitHub 的乾淨部署版本。此資料夾不包含正式資產資料、備份資料、`.env` 或私人 `finance-data.js`。

## Included Files

- `index.html`
- `styles.css`
- `app.js`
- `server.py`
- `requirements.txt`
- `scripts/`
- `data/example-*.json`

## Private Data Rules

不要提交自己的正式資料：

- `data/*.json`
- `data/*.txt`
- `data/*.js`
- `data/backups/`
- `data/test-backups/`
- `.env`
- `finance-data.js`
- `*.db`
- `*.sqlite`
- `*.sqlite3`

`.gitignore` 已經保護上述路徑。範例檔 `data/example-*.json` 會保留在 Git 內，方便看格式。

## Login Password

網站使用瀏覽器原生登入視窗保護所有頁面與 API。

必填環境變數：

```bash
ASSET_DASHBOARD_PASSWORD="replace-with-a-strong-password"
```

可選環境變數：

```bash
ASSET_DASHBOARD_USERNAME="admin"
```

如果沒有設定 `ASSET_DASHBOARD_PASSWORD`，網站會拒絕登入，避免公開部署後裸露資料。

## Run Locally

```bash
pip install -r requirements.txt
ASSET_DASHBOARD_PASSWORD="local-password" \
python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

打開：

```text
http://127.0.0.1:8000/
```

## Deploy to Render / Railway

Build command：

```bash
pip install -r requirements.txt
```

Start command：

```bash
uvicorn server:app --host 0.0.0.0 --port $PORT
```

Environment variables：

```text
ASSET_DASHBOARD_PASSWORD=replace-with-a-strong-password
ASSET_DASHBOARD_USERNAME=admin
ASSET_DB_PATH=/var/data/app.db
```

如果使用 Supabase，另外加上：

```text
SUPABASE_DB_URL=postgresql://postgres.your-project:your-password@aws-0-region.pooler.supabase.com:6543/postgres
```

### Render Persistent Disk

正式資料預設存進 `data/app.db`。Render Free 沒有 Persistent Disk，資料可能在重新部署、服務重建或休眠後消失。

Render 建議設定：

1. 在服務頁面新增 Disk
2. Mount path 設為 `/var/data`
3. Environment 加上：

```text
ASSET_DB_PATH=/var/data/app.db
```

Railway 或其他平台也可以用 `ASSET_DB_PATH` 指到持久化磁碟路徑。

## Import Your Own Data

Public Edition 內建 Demo Mode。正式資料會優先從 SQLite 讀取；SQLite 沒資料時，首頁和讀取 API 會自動使用範例資料：

- `data/example-portfolio.json`
- `data/example-transactions.json`
- `data/example-dividends.json`

SQLite 正式資料來源：

- `accounts`
- `transactions`
- `dividends`
- `portfolio_snapshots`
- `net_worth_history`
- `prices`

可用 API：

```text
GET /api/db/status
GET /api/db/export-json
POST /api/db/import-json
POST /api/db/rebuild-portfolio
```

以上 API 需要登入。只有 `/api/health` 和 `/api/auth-debug` 不需要登入。

### Free Tier Backup / Restore

如果暫時沒有 Render Persistent Disk，可以使用手動備份流程：

1. 首次部署後登入網站
2. 按「匯入備份」匯入你 MacBook 上的備份 JSON
3. 網站會把資料寫入 SQLite 並顯示真實資產
4. 每次更新資料後，按「匯出備份」下載 JSON 到自己的 MacBook
5. 如果 Render 重新部署後資料消失，再登入網站並重新匯入備份 JSON

備份 JSON 會包含：

- `accounts`
- `transactions`
- `dividends`
- `prices`
- `net-worth-history`
- `portfolio`

也可以用 API 下載備份：

```bash
curl -u admin:your-password \
  https://your-render-url/api/db/export-json \
  -o wealth-dashboard-backup.json
```

上傳備份：

```bash
curl -u admin:your-password \
  -F backup=@wealth-dashboard-backup.json \
  https://your-render-url/api/db/import-json
```

若本機已有舊版正式 JSON，也可以先把這些檔案放在本機或 Render Disk 的 `data/` 目錄：

- `data/portfolio.json`
- `data/transactions.json`
- `data/dividends.json`
- `data/accounts.json`
- `data/prices.json`
- `data/net-worth-history.json`

再呼叫：

```bash
curl -u admin:your-password -X POST https://your-render-url/api/db/import-json
```

匯入後會寫入 SQLite 並重新計算 portfolio。正式 JSON 檔仍然不要 commit 到 GitHub。

範例檔放在 `data/`：

- `data/example-portfolio.json`
- `data/example-transactions.json`
- `data/example-dividends.json`
- `data/example-accounts.json`
- `data/example-prices.json`
- `data/example-net-worth-history.json`

本機測試匯入時，可以建立正式檔名：

```bash
cp data/example-transactions.json data/transactions.json
cp data/example-dividends.json data/dividends.json
cp data/example-accounts.json data/accounts.json
cp data/example-prices.json data/prices.json
cp data/example-portfolio.json data/portfolio.json
cp data/example-net-worth-history.json data/net-worth-history.json
```

然後把內容換成自己的資料。

重新產生首頁資料：

```bash
curl -u admin:local-password -X POST http://127.0.0.1:8000/api/db/rebuild-portfolio
```

注意：Render / Railway 的一般檔案系統不適合長期保存正式資產資料。正式部署請使用 Persistent Disk，並設定 `ASSET_DB_PATH`。

## Supabase Cloud Database

Supabase 版會優先使用 PostgreSQL。若 Supabase 連線失敗、環境變數錯誤或服務暫時不可用，網站會自動改用 SQLite，不會影響既有 Render 網站開啟。

支援的環境變數，擇一設定即可：

```text
SUPABASE_DB_URL=your-supabase-postgres-url
SUPABASE_DATABASE_URL=your-supabase-postgres-url
DATABASE_URL=your-supabase-postgres-url
```

建議使用 `SUPABASE_DB_URL`，避免和其他平台預設資料庫設定混淆。

### Supabase 設定步驟

1. 到 [Supabase](https://supabase.com/) 建立新 Project。
2. 進入 Project Settings -> Database。
3. 找到 Connection string，選擇 URI 格式。
4. 建議 Render 使用 Transaction pooler 連線字串，連接埠通常是 `6543`。
5. 將連線字串中的 `[YOUR-PASSWORD]` 換成 Supabase database password。
6. 到 Render 服務的 Environment 新增：

```text
SUPABASE_DB_URL=你的 Supabase PostgreSQL 連線字串
```

7. 保留 SQLite fallback：

```text
ASSET_DB_PATH=data/app.db
```

8. 重新部署 Render。
9. 登入網站後看首頁「Current DB」卡片，若顯示 `Supabase PostgreSQL` 代表已使用雲端資料庫。
10. 若顯示 `SQLite` 並出現 fallback 提示，代表 Supabase 連線失敗，網站仍可正常使用，但資料會暫存在 SQLite。

### 資料遷移到 Supabase

方式一：用網站按鈕

1. 先在 Render 設好 `SUPABASE_DB_URL` 並重新部署。
2. 登入網站。
3. 按「遷移 Supabase」。
4. 完成後首頁 Current DB 應顯示 `Supabase PostgreSQL`。

方式二：用 API

```bash
curl -u admin:your-password \
  -X POST https://your-render-url/api/db/migrate-to-supabase
```

方式三：重新匯入備份

1. 設定 Supabase 環境變數並重新部署。
2. 登入網站。
3. 按「匯入備份」，選擇 MacBook 上的備份 JSON。
4. 匯入後資料會寫入目前可用的正式資料庫。Supabase 正常時會寫入 Supabase；Supabase 失敗時會自動寫入 SQLite。

### 資料狀態卡

首頁會顯示：

- `Current DB`：目前使用 Supabase PostgreSQL 或 SQLite
- `Last Backup`：上次匯出備份時間
- `Last Price Update`：上次更新股價時間
- `Data Status`：正式資料、範例資料或 fallback 狀態

### 備份提醒

即使使用 Supabase，仍建議定期按「匯出備份」下載 JSON 到自己的 MacBook。若超過 7 天沒有匯出備份，首頁會提醒。

## Safety Checklist Before GitHub Push

```bash
git status --short
git check-ignore -v data/transactions.json data/portfolio.json data/backups/example.json .env finance-data.js data/app.db
```

確認正式資料仍被忽略後，再推送 GitHub。
