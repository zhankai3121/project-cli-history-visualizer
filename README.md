# CLI History Visualizer

跨專案檢視 Claude Code 的歷史 prompt 與實際進度。本機執行，資料不離開這台機器。

一個本機網頁，回答三個問題：

- **「那句 prompt 是什麼時候、在哪個專案下的？」** — 全文搜尋所有歷史 prompt
- **「這個專案上次做到哪？」** — 自動從對話紀錄抽出目標／狀態／下一步
- **「講的事情真的做完了嗎？」** — 用 git 狀態驗證，不只信對話

---

## 為什麼需要這個

`~/.claude/` 底下有兩份**保存期完全不對等**的資料：

| 檔案 | 保存期 | 內容 |
|---|---|---|
| `history.jsonl` | **永久** | 每一則你打過的 prompt：原文、時間、專案真實路徑、sessionId |
| `projects/<slug>/<sessionId>.jsonl` | **預設 30 天** | 完整過程：工具呼叫、改了哪些檔、AI 的結論與進度摘要 |

`cleanupPeriodDays` 預設 30 天，過期的 transcript 會被自動刪除。

開發這個工具時在自己機器上實測：**87 個 session 只有 15 個還留著過程，其餘 72 個只剩一行 prompt**。改了什麼、結論是什麼，全部消失。

所以這個工具的核心不是搜尋，是**在 transcript 被刪之前把它蒸餾進本機 SQLite** —— 一台黑盒子記錄器。搜尋與視覺化是蒸餾之後的自然結果。

### 先止血

這工具只能保存「還沒被刪的」。要停止流失，在 `~/.claude/settings.json` 加一行：

```json
{ "cleanupPeriodDays": 3650 }
```

---

## 安裝與執行

需求：**Python 3.10+**、**SQLite 3.34+**（FTS5 trigram tokenizer 需要）、`git`（選用，用於紅綠燈）。

```bash
git clone https://github.com/zhankai3121/project-cli-history-visualizer.git
cd project-cli-history-visualizer

python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux

.venv\Scripts\python server.py                     # -> http://127.0.0.1:8787
```

首次啟動會自動建立索引（本機實測約 1 秒 / 700 則 prompt）。瀏覽器會自動開啟。

確認 SQLite 版本夠新：

```bash
python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute(\"create virtual table t using fts5(x, tokenize='trigram')\"); print('FTS5 trigram OK', sqlite3.sqlite_version)"
```

索引指令：

```bash
python indexer.py              # 增量（只讀 jsonl 新增的部分，git 狀態吃快取）
python indexer.py --force-git  # 增量，但重讀所有 repo 的 git 狀態
python indexer.py --full       # 砍掉重建（專案根目錄設定會保留）
```

**git 狀態是有快取的。** 每個 repo 要跑三次 git 指令，在 WSL 的 UNC 路徑上單一 repo
就要 0.5–0.9 秒（本機實測 16 個 repo 共 3.8 秒）。所以用 `.git/{HEAD,index,packed-refs,refs}`
的 mtime 當指紋，沒變就跳過 —— 實測 3.86s → 0.27s。

取捨要講清楚：**純粹改工作區的檔案不會動到 `.git`**，所以「未提交檔案數」最多可能
落後 `GIT_TTL_SECONDS`（預設 600 秒）。按網頁上的 `↻`、跑 `--force-git` 或 `--full`
都會強制重讀。

實作上有個關鍵細節：指紋必須在**跑完 git 之後**才取 —— `git status` 自己會重寫
`.git/index`，先取的話下次比對必定不同，快取等於沒做。`tests/test_git_cache.py`
有一條專門守這件事。

跑測試：

```bash
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests/ -q
```

### 設定專案資料夾

標頭的 **📁 資料夾** 會開啟資料夾選擇器：導覽到你放專案的地方 → 「＋加入目前資料夾」。
可以加多個根目錄，設定存在 DB，重開 server 還在。

移除根目錄時，當初只靠掃描登記、又沒有任何 CLI 資料的專案會一併刪掉 ——
不然設錯一次（例如選了整個家目錄）就會永久留下一堆 `AppData`、`Downloads` 之類的假專案。
有 prompt 或 session 的專案永遠保留。

### WSL 資料夾（Windows）

選擇器的 **💾 磁碟機 / WSL** 會列出磁碟機與所有**正在執行**的 WSL 發行版
（靠 `wsl.exe -l -q` 動態取得，不寫死名稱）。點發行版就能像一般資料夾一樣往下走：

```
💾 磁碟機 / WSL  →  WSL · <發行版>  →  home  →  <使用者>  →  projects  →  ＋加入
```

也可以直接在下方輸入框貼 UNC 路徑：

```
\\wsl.localhost\<發行版>\home\<使用者>\code
\\wsl$\<發行版>\home\<使用者>\code          # 舊版 Windows 用這個
```

發行版要在執行中才讀得到（`wsl -l -v` 看 STATE）。
路徑拼接一律在後端做，所以 UNC、網路磁碟機、非 Windows 路徑都不會拼錯。

`CLIHV_PROJECT_ROOT` 環境變數只作為**首次啟動的預設值**，之後以網頁上的設定為準：

```bash
export CLIHV_PROJECT_ROOT="/path/to/your/projects"   # macOS / Linux
$env:CLIHV_PROJECT_ROOT = "D:\work"                  # PowerShell
```

Server 每次提供專案清單時會順手 stat 一次資料夾，所以資料夾刪掉／放回來會即時反映。重新掃描 transcript 按右上角 `↻`。

---

## 進度是怎麼抽出來的

**全部是確定性規則，不呼叫任何 LLM。** Claude Code 其實自己就在寫進度摘要，只是沒人讀它：

| 來源記錄 | 內容 |
|---|---|
| `type:"system"` `subtype:"away_summary"` | 閒置時自動寫的摘要。英文格式是 `Goal: … Next: …`；中文是散文，用「下一步是／接下來／現在只差」當轉折 |
| `type:"user"` `isCompactSummary:true` | `/compact` 產生的結構化整段摘要（Primary Request / Pending Tasks / Next Step…） |
| `type:"ai-title"` | session 標題 |
| `type:"last-prompt"` | 最後一則真人 prompt |
| `type:"cost-state"` | `totalLinesAdded` / `totalLinesRemoved` / `totalDuration` |

### 子代理

`projects/<slug>/<sessionId>/subagents/` 底下的 transcript 本機實測有 216 MB，
是主 transcript 的 8 倍。**不整包索引** —— 只抽三樣：

| 抽什麼 | 從哪來 |
|---|---|
| 交辦內容、agent 類型、模型 | `agent-<id>.meta.json` |
| 回報給主線的結果 | 該 transcript 最後一則 assistant 發言 |
| 改過的檔案、跑過的指令 | `tool_use` 區塊 |

中間過程（每一步的搜尋、讀檔、思考）不存，那才是那 200 MB 的來源。

子代理的產出會**併進主線統計但標記 `via_agent`**，需要時分得開。這個差距比預期大 ——
本機一個專案改過的 555 個檔案裡，**516 個是子代理做的**，之前完全沒被計入。

回報內容進 `subagent_fts`，搜尋的 `scope=agent` 可以單獨查。

再疊上從 `tool_use` 算出來的硬事實：

| 訊號 | 抽法 |
|---|---|
| 改了哪些檔 | `tool_use.name ∈ {Edit, Write, NotebookEdit}` → `input.file_path` 去重 |
| 跑了什麼 | `Bash` / `PowerShell` 的 `command`，依首動詞分類（git／test／build／install） |
| commit | command 含 `git commit` → 抓 `-m` 或 heredoc 訊息 |
| 卡關 🚩 | 同一檔案被 Edit ≥3 次，或 tool_result 帶 `is_error` |

最後把使用者自己手寫的 `~/.claude/projects/<slug>/memory/{brain.md,MEMORY.md}` 當**對照欄**吃進來。

**每張卡片都標示來源**，所以你一眼知道某條進度是 AI 自動摘的、還是自己寫的。兩邊不一致時反而是最有用的訊號 ——「brain.md 說在做 M1，但實際三週沒動過那個檔」。

### git 紅綠燈

對話講的事情不等於真的落地。transcript 裡通常只抓得到極少數 `git commit`（多數 commit 不是透過工具下的），所以直接問 repo：

| 燈 | 判準 |
|---|---|
| 🟢 綠 | 最後一次 commit 在最後一則 prompt 之後 → 已落實 |
| 🟡 黃 | 工作區有未提交的變更 → 正在產出中 |
| ⚪ 灰 | 談過但 commit 沒跟上 |
| — | 不是 git repo，無從判斷 |

---

## 操作

| 動作 | 方式 |
|---|---|
| 全域搜尋 | <kbd>Ctrl</kbd>+<kbd>K</kbd> |
| 限定專案搜尋 | 搜尋框打 `@專案名 關鍵字` |
| 直接跳某專案 | 打 `@專案名` 後停住 |
| 找「大概八月中那次」 | 底部熱度圖點那一天 |
| 回到總覽 | <kbd>Esc</kbd> |
| 續接某次對話 | session 詳情 → 複製 `claude --resume <id>` |
| 開專案 | 專案頁 → 在 VS Code 開啟 |
| 設定專案資料夾 | 標頭 `📁 資料夾` |
| 字級調整 | 標頭 `A−` / `A+`（倍率 0.7–2.0，存 localStorage） |
| 外觀 | 標頭下拉選單：**預設 · 亮** / **預設 · 暗** / 10 種主題 |

排序：最近活動／停滯最久／紅旗優先／prompt 量
篩選：有未完成的 Next／30 天內活躍／有紅旗／過程已蒸發／顯示已刪除的專案／顯示容器目錄／只看有 CLI 紀錄

## 支援的 CLI

| CLI | 資料來源 | 偵測方式 |
|---|---|---|
| **Claude Code** | `~/.claude/history.jsonl` + `~/.claude/projects/<slug>/*.jsonl` | 一律啟用 |
| **OpenAI Codex** | `~/.codex/history.jsonl` + `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl` | 有 `sessions/` 或 `history.jsonl` 才啟用；沒裝就整段跳過 |

`CODEX_HOME` 環境變數可以改 Codex 的家目錄。兩邊的 prompt 與 session 都帶 `tool`
欄位分開標記，卡片與 session 列會顯示來源。

### Codex 格式的兩個關鍵事實

格式來自 [openai/codex](https://github.com/openai/codex) 的原始碼（`HistoryEntry` struct
與 rollout 檔名規則，Apache-2.0）與公開的 rollout 樣本。

**1. 真人打的字在 `event_msg` / `user_message`，不是 `response_item` 的 `role=="user"`。**

那些 `role=="user"` 的 `response_item` 是系統注入的東西 —— AGENTS.md 全文、
`<environment_context>`、`<user_instructions>`。拿 `role` 判斷會把整份專案指示
當成使用者 prompt 灌進資料庫。這跟 Claude 那邊 `<system-reminder>` 是同一類陷阱，
`tests/test_codex.py` 有一組測試專門守這條。

**2. Codex 的 `history.jsonl` 沒有 `cwd`**（Claude 的有）。

`HistoryEntry` 只有 `{session_id, ts, text}` 三個欄位，專案歸屬只能靠 rollout 的
`session_meta.cwd` 用 `session_id` 對回來 —— 所以索引順序是 rollout 先跑、history 後補。
rollout 已經不在的 session 仍然留得住 prompt，只是沒有專案歸屬。

順帶一提，Codex 的 `session_meta` 自帶 `git: {branch, commit}`，比 Claude 那邊多這層資訊。

### 專案清單的來源

專案來自兩條互補的管道，用 `has_history` / `is_scanned` 分開標記：

1. **跑過 CLI 的目錄** —— `history.jsonl` 的 `project` 欄位與 transcript 每行的 `cwd`。這是**進度資料的唯一來源**。
2. **資料夾掃描** —— 你設定的根目錄底下的專案資料夾。沒跑過 CLI 的會標 `📂 無 CLI 紀錄`，只是登記存在，沒有進度。

偵測規則：

| 深度 | 認定 |
|---|---|
| 根目錄下第 1 層 | 一律算專案 |
| 第 2 層 | 只認自帶 `.git` / `.claude`，或已經有 CLI 紀錄的 |

第 2 層設限是為了不讓 `src/`、`tests/` 被當成專案。掃描與瀏覽都會跳過 `node_modules`、`.venv`、`dist`、`build` 等常見雜項目錄。

其他行為：

- 資料夾被刪掉 → 自動從網頁消失（**軟刪除**：`exists_on_disk=0`，歷史留著，放回來就復原）
- 移除根目錄時，只靠掃描登記、又沒有任何 CLI 資料的專案會一併刪掉
- 想切回「只看跑過 CLI 的」→ 篩選列的 **只看有 CLI 紀錄**

**非專案路徑預設隱藏**（篩選列的「顯示非專案路徑」可以叫出來），兩類：

| 類型 | 判準 | 卡片標示 |
|---|---|---|
| 容器目錄 | 任一根目錄本身與其祖先，例如 `…\Project`、`…\Desktop` | `📁 容器` |
| 系統目錄 | `C:\Windows`、`C:\Program Files`、`/usr`、`/etc`、`/var`… （WSL 的 UNC 路徑會先剝掉 `\\wsl.localhost\<發行版>` 前綴再比） | `⚙ 系統目錄` |

兩者都是「在那個目錄下開過終端機跑 CLI」的副作用，不是真的專案。

### 分組檢視

工具列的**分組**選單：不分組 ／ 依資料夾 ／ 依 git 狀態。依資料夾會把
Windows 主目錄、巢狀子專案、WSL 的專案各自收成一組，可逐組收合，選擇存 localStorage。

---

## 外觀主題

10 種主題，色票與元件語言取自 **[Fable 5.1 · 100 HTML Files](https://miaai-lab.github.io/Fable-5.1-100-HTML-Files/)**
（[miaai-lab](https://github.com/miaai-lab/Fable-5.1-100-HTML-Files)）隨機抽樣的 10 支 demo。
同一批風格套用到 CRM 模板的版本可見 [Fable 5.1 × CRM · 100 種風格](https://zhankai3121.github.io/fable-crm-100-styles/)。

**改的不只顏色** —— 每個主題各自定義卡片背景（可為漸層）、邊框粗細、陰影、內框、圓角、
毛玻璃、整頁漸層底、質感疊層、字體與字距。下表的「來源」是抽中的那支 demo，
編號即原作的檔名，可以到上面的風格庫對照原始設計：

| 主題 | 來源 demo | 元件特徵 |
|---|---|---|
| 石墨金屬 | `005-liquid-metal-blob` | 拉絲漸層面板、上緣 1px 高光、硬角、全大寫寬字距、SVG 噪點 |
| 珊瑚紫霧 | `009-morphing-gradient-mesh` | `blur(16px)` 毛玻璃、20px 大圓角、圓形熱度圖格子、背景暈染色團 |
| 黃銅熔岩 | `027-lava-lamp-css` | 26px 燈罩圓角、暖色輝光、襯線字、45° 斜紋壁面 |
| 琥珀曼陀羅 | `028-generative-mandala` | 深玻璃、0.3em 超寬字距、中心放射暈 |
| 蒸汽波 | `034-vaporwave-sunset` | 零圓角、2px 粗邊、霓虹 text-shadow、青／粉雙向網格、等寬字 |
| 報紙頭版 | `055-newspaper-front-page` | 零圓角、`3px double` 報頭、卡片內雙框、紙紋 multiply、襯線字 |
| 原子時代 | `071-atomic-age-retro-futurism` | 3px 粗框 + 6px 硬投影（hover 位移）、膠囊 chip、稿紙橫線 |
| 靜水禪 | `093-pomodoro-zen-timer` | 完全無邊框、24px 大圓角、44px 柔霧陰影 |
| 熱顯像 | `096-thermal-vision-heatmap` | 掃描線疊層、儀器內描邊、等寬全大寫、色階是真的熱力圖 |
| 靛藍水面 | `099-water-ripple-reflection` | 水面三段漸層底、銀線玻璃、深投影 |

主題主要靠重新定義 CSS 變數。少數幾個主題另外有 descendant 規則做視覺細節
（報紙的內框、原子時代的硬投影位移、蒸汽波的霓虹光暈），改的是 `padding` /
`transform` / `text-shadow` / `box-shadow`。

**主題不會碰版面原語** —— `display`、`grid-template-*`、`width`、`overflow`、
`position` 這類屬性一律禁止，所以換主題不可能把 RWD 弄壞。
`tests/test_web.py` 會逐條掃描每個主題規則（含 descendant）強制這件事。

明暗與主題**共用同一個選單**，互斥：選「預設 · 亮/暗」走 `data-theme`（沿用預設配色），
選主題走 `data-skin` 並清掉 `data-theme` —— 主題自己定義了全部色票，兩個屬性同時掛著
只會互相干擾。

沒選過之前跟隨系統的 `prefers-color-scheme`，而且**不寫入** localStorage；選過才存。

### 響應式

盡量用 CSS 內建機制，斷點只留真的沒辦法表達的：

| 機制 | 負責 |
|---|---|
| `--fs: clamp(17px, 0.657vw + 14.54px, 24px)` | 字級隨視窗**連續**縮放。係數是解 375px→17px、1440px→24px 兩點得到的 |
| `.split{display:flex; flex-wrap:wrap}` | 側欄放不下就自己換行（約 870px），不需要斷點 |
| `minmax(min(20rem,100%),1fr)` | 卡片網格的軌道下限被視窗夾住，窄螢幕不會溢出 |

剩下三個斷點：**900px** 堆疊後的高度（`.list` 78vh→38vh，兩個滿版高度不能相加，
這是視窗相關的，沒有內建機制可表達）、**640px** 搜尋框獨占一行與進度三行改單欄、
**420px** 間距收緊。另有 `any-pointer: coarse`（觸控裝置放大點擊區，用 `any-pointer`
是因為觸控筆電回報 `pointer: fine`）與 `prefers-reduced-motion`。

`A−`/`A+` 調的是**倍率** `--fs-scale`（0.7–2.0），不是絕對 px —— 在寬螢幕調大之後
換到手機，仍然吃得到 `clamp()` 的響應式縮放。

### 想自己加主題

`web/index.html` 的 `:root[data-skin="..."]` 區塊就是全部。抄一份改變數即可，不用碰任何版面規則：

```css
:root[data-skin="mine"]{
  --bg:…; --panel:…; --line:…; --ink:…; --dim:…; --accent:…; --on-accent:…;
  --flag:…; --ok:…; --gone:…; --cell0:…; --cell1:…; --cell2:…; --cell3:…; --cell4:…;
  /* 以下才是讓主題有個性的部分 */
  --radius:…; --chip-radius:…; --cell-radius:…; --border-w:…;
  --card-bg:…; --shadow:…; --hover-shadow:…; --card-inset:…; --blur:…;
  --page-bg:…; --texture:…; --texture-op:…; --texture-blend:…;
  --title-transform:…; --title-spacing:…; --label-transform:…; --font:…;
}
```

再到標頭的 `<select id="skin">` 加一個 `<option value="mine">名稱</option>` 就完成。

---

## 架構

```
server.py            FastAPI，port 8787
indexer.py           掃 ~/.claude -> SQLite（增量，靠 mtime/size/offset）
parser.py            Claude Code 的 JSONL / Markdown 判別式（規格見 docs/jsonl-schema.md）
codex.py             OpenAI Codex 的 rollout / history 解析
schema.sql           資料表與 FTS5 定義
web/index.html       單檔前端，無 build step，零外部依賴
tests/               pytest（86 個）—— 用合成資料，不碰真實的 ~/.claude
docs/jsonl-schema.md 實測的 transcript schema —— 改 parser 前先讀這份
docs/plan.md         當初的設計計畫與取捨紀錄
index.db             索引產物（.gitignore），隨時可由 ~/.claude 重建
backup/              每次索引順手備份 history.jsonl（.gitignore，含真實 prompt）
```

後端只有 `fastapi` + `uvicorn` 兩個依賴，其餘全 stdlib。前端沒有 build step、沒有 CDN、沒有框架。

### 資料表

```
project(real_path UNIQUE, display_name, first_seen, last_seen, session_count,
        prompt_count, exists_on_disk, has_history, is_scanned, is_git, is_container,
        git_branch, git_last_ts, git_last_msg, git_dirty, git_commits, vanished_at)
app_config(key PK, value)                      -- 目前只存 project_roots
session(id PK, project_id, started_at, ended_at, prompt_count, title,
        transcript_state)                      -- live | gone
prompt(session_id, project_id, ts, seq, text, is_slash, source, pasted)
turn(session_id, prompt_id, ts, assistant_summary, tools_json, has_error)
file_touch(session_id, project_id, ts, path, verb)
command_run(session_id, project_id, ts, command, kind)
commit_ref(session_id, project_id, ts, message)
progress_signal(session_id, project_id, ts, kind, goal, state, next_step, body, origin)
scan_state(path PK, mtime, size, last_offset)  -- 增量索引游標
```

FTS5 虛擬表：`prompt_fts` / `turn_fts`，`tokenize='trigram'`。

### 增量索引

jsonl 是 append-only，所以 `scan_state` 記錄每個檔案的 `(mtime, size, last_offset)`，下次直接 `seek` 續讀。沒變動的檔案零成本。

---

## API

| 端點 | 說明 |
|---|---|
| `GET /api/overview?include_gone=&include_containers=&only_history=` | 專案進度卡（會順手掃描資料夾並對帳存在與否） |
| `GET /api/roots` · `POST /api/roots` | 讀取／整批覆寫要掃描的專案根目錄 |
| `GET /api/browse?path=` | 列出某目錄的子目錄（`{name, path}`，路徑由後端拼）。**只回目錄名稱，不讀任何檔案內容**；`path` 留空時 Windows 回磁碟機 + 執行中的 WSL 發行版 |
| `GET /api/project/{id}` | 專案詳情 + session 列表 |
| `GET /api/session/{id}` | prompt / turn / 改檔 / 指令 / commit / 進度訊號 |
| `GET /api/search?q=&limit=&scope=&since=&until=&slash=` | 全文搜尋（FTS5，短查詢自動退回 LIKE）。`scope`=all/prompt/reply/agent；`since`/`until` 是 `YYYY-MM-DD`，`until` 含當天；`slash`=all/only/exclude（only 時不查回覆與子代理）。每筆命中多一個 `snippet`：已跳脫的片段，命中處包 `<mark>` |
| `GET /api/recent?limit=` | 跨專案最近動態 |
| `GET /api/heatmap` | 每日 prompt 數 |
| `GET /api/day/{YYYY-MM-DD}` | 某一天的所有 prompt |
| `POST /api/reindex` | 增量重新索引 |

---

## 隱私

- **完全本機執行**，綁定 `127.0.0.1`，不對外連線、不上傳任何資料。
- `index.db` 與 `backup/` 含你的真實 prompt 原文，**已列入 `.gitignore`**，不會進版控。
- 這個 repo 本身不含任何個人資料。

---

## 實作時踩過的坑

記在這裡，因為每一個都是靜默失效 —— 不會報錯，只會給你錯的結果。

1. **FTS5 trigram 對 2 字中文回傳 0 筆而不報錯。** 看起來就像「真的沒這筆資料」。查詢層必須偵測 `len(query.strip()) < 3` 時改走 `LIKE '%q%'`。全案最難察覺的一個。

2. **slug 不可逆。** `~/.claude/projects/` 的目錄名把所有非 ASCII 字元逐字轉成 `-`，所以 `myapp\完整規劃`、`myapp\問題規劃`、`myapp\套版整合` 三個不同目錄會產生**完全相同**的 slug。專案 key 一律用 `cwd` / `project` 真實路徑，**絕不從 slug 反推**。

3. **`type:"summary"` 在現行 CLI 版本不存在。** 整個 `~/.claude/projects` 樹 grep 0 筆命中。舊資料照著寫會靜默失效。session 標題要用 `type:"ai-title"`。

4. **hook 注入不在 `type:"user"` 裡。** 一律是獨立的 `type:"attachment"` 記錄。對全樣本的 user payload 搜 `system-reminder` 命中率恆為 0 → 不需要寫任何清洗正規式。真人 prompt 的判別鍵是 `origin.kind == "human"`。

5. **同一個 sessionId 可能出現在多個 slug 目錄下**（worktree / code-review 產物）。session 表以 sessionId 為主鍵，專案歸屬取該 session 第一筆 `cwd`。

6. **`parentUuid` 不保證能在單一檔案內解析。** 查無父節點時要優雅降級為根節點。

7. **JSONL 尾端可能有半行** —— 你正在另一個視窗用 CLI。解析失敗就不推進 `last_offset`，下次重讀。

8. **subagent transcript 體積是主 transcript 的 5 倍**（本機實測 131 MB vs 25 MB），訊噪比極差，預設不索引。

9. **Windows 編碼。** 所有 `open()` 明寫 `encoding="utf-8"`；任何 stdout 輸出前設 `PYTHONIOENCODING=utf-8`，否則 Big5 主控台直接炸。

10. **欄位跨版本不穩定。** 樣本橫跨 7 個 CLI 版本，欄位集合差異很大。除 `type` / `message` / `timestamp` / `sessionId` / `version` 外，一律 `dict.get(key, default)` 保守存取。

完整實測 schema 見 [`docs/jsonl-schema.md`](docs/jsonl-schema.md)。

---

## 授權

MIT
