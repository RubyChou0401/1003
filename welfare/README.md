# 員工職福好康站

企業內部「員工職福特約廠商優惠查詢與管理平台」。以**優惠生命週期**為核心：建立 → 送審 → 發布 → 即將到期 → 續約 → 自動下架 → 新版本。

技術：Python 3 + Flask + openpyxl。資料庫預設 SQLite（單一檔案，免額外服務）；設定環境變數 `DATABASE_URL` 即改用 PostgreSQL／Supabase。日期時間一律 Asia/Taipei（UTC+8）。

## 啟動

```bash
cd welfare
pip install -r requirements.txt
python seed_demo.py          # 選用：建立示範資料（正式環境請勿執行）
python app.py                # http://127.0.0.1:5000
python tests/test_smoke.py   # 端到端測試
```

預設系統管理員：`admin` / `Admin1234`（可用環境變數 `WELFARE_ADMIN_PASSWORD` 指定；首次登入強制改密碼）。
示範帳號：`H0001`（HR）、`E0001`（員工），密碼 `Demo12345`。

### 使用 Supabase（PostgreSQL）
1. Supabase 專案 → **Connect** → 複製 **Session pooler** 連線字串（格式 `postgresql://postgres.<ref>:<密碼>@aws-0-<區域>.pooler.supabase.com:5432/postgres`）。
2. 在執行網站的主機設定環境變數：`DATABASE_URL="<連線字串>"`（含密碼，**不要**放進程式碼或 GitHub）。
3. 資料表已建立並啟用 RLS（不開放公開 API，僅後端連線可存取）；首次啟動會自動寫入預設角色、分類、地區與 `admin` 帳號。
4. 備份：Supabase 平台每日備份＋系統內「立即備份」（匯出 JSON 至 `backups/`，含密碼雜湊，請限制存取）。
5. 測試：`DATABASE_URL=... python tests/test_smoke.py`（會**清空**該資料庫 public schema，請只對測試資料庫執行）。

### 正式上線注意
- 請放在 HTTPS 反向代理（nginx 等）之後，並設定 `WELFARE_HTTPS=1`（Session Cookie 加 Secure）；以 gunicorn 等執行 `app:app`。
- `WELFARE_SECRET` 指定固定金鑰（未設定時會產生 `data/secret.key`）。`WELFARE_DB`、`WELFARE_BACKUP_DIR` 可改資料與備份位置。
- `data/`、`backups/`、`uploads/` 含個資，請限制存取並納入主機備份。

## 結構
| 檔案 | 說明 |
|---|---|
| `db.py` | 資料表、預設資料、稽核 `audit()` |
| `lifecycle.py` | 顯示狀態（有效／即將到期／已過期）自動計算、每日自動下架、提醒 |
| `excel.py` | Excel 匯入：解析→檢核→預覽→確認；錯誤列不寫入 |
| `views_front.py` | 員工端：登入、首頁、搜尋、詳細頁、公告、我的 |
| `views_admin*.py` | 後台：Dashboard、員工、廠商／門市、優惠審核／續約／版本、公告、統計、權限、稽核、備份 |

## 重要規則如何落實
- **歷史版本不可覆蓋**：內容只有「草稿」可編輯；續約一律建立新版本（`offer_versions`），舊版只標記「已續約」。
- **到期自動判斷**：有效／即將到期／已過期由日期即時計算；員工前台查詢條件直接排除過期，並每小時執行自動下架作業（寫入稽核與審核紀錄，不刪資料）。
- **關聯用 ID**：廠商以編號、優惠／門市以主鍵關聯；不存在或停用的廠商不可建立優惠，門市必須屬於該廠商。
- **匯入保護**：預覽不寫入；確認前自動備份；確認時重新檢核；匯入只能建立草稿／待審核，不覆蓋已發布版本；暫存的初始密碼在確認或取消後清除。
- **資安**：密碼雜湊、錯誤次數鎖定、閒置登出、首登改密碼、CSRF、RBAC（後台每個路由驗證權限）、員工只能讀自己的資料（`/me` 不接受 id）、上傳限圖片格式、友善錯誤頁。
- **統計**：只呈現彙總；瀏覽與搜尋事件不記錄個人，僅登入事件記錄員工（用於登入人數）。

## 第一階段未包含
LINE／Email 通知、收藏／最近瀏覽、廠商聯絡紀錄等（列為第二階段）。員工登入目前為員工編號＋密碼（未串接公司 SSO）。
