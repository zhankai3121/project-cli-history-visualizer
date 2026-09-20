# plan-v2 — 11 項功能實作計畫

每個 Fx 交給一個全新 context 的 implementer，只拿本節 + 程式碼。行號以 2026-09-20 HEAD (6dd8ad5) 為準（批次 0 插入錨點後行號會位移，以錨點與函式名為準）。

已決定的事項（2026-09-20）：F1 熱度用 input+cache_create+output，cache_read 只放 title；F7 以 real_path 為 key；F5 `--watch` 與排程器兩種都寫進 README；F10 只做 `cli.py`。

## 共通規則（每節都適用，不再重複）
- 後端只准 stdlib + fastapi + uvicorn。所有 `open()` 明寫 `encoding="utf-8"`；任何 CLI 進入點第一行 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`（Big5 主控台會炸）。
- 新表寫進 `schema.sql`（`CREATE TABLE IF NOT EXISTS`，`indexer.connect()` 每次 executescript 所以舊 DB 自動有表）；新欄位加進 `indexer.migrate()` 的 `wanted` dict（indexer.py:90-117）。舊 DB 不跑 `--full` 也要能用；若新資料要從已索引過的 transcript 回填，必須另寫一次性 backfill（不要清 `scan_state`，那會讓 turn/file_touch 重複插入，它們沒有 UNIQUE）。
- 新 API 放在 server.py 的 `# @Fx-api` 錨點下；前端 CSS/JS 放在 index.html 的 `/* @Fx-css */`、`/* @Fx-js */` 錨點下（§批次 0）。既有函式只准改「觸點表」列的那些。
- 不准動 `:root[data-skin=…]` 規則；新 CSS 不用 data-skin 選擇器（test_web 的 LAYOUT_PROPS 只掃主題規則，一般規則可用 display/grid）。字級只用 rem。`$("#id")` 抓的 id 必須存在於 HTML（test_web 自動掃）。
- 測試用 `tests/conftest.py` 的 `fake_home`/`indexed`/`client` fixture 與 `jsonl()/user_prompt()/assistant()`；不碰真實 `~/.claude`。完成後 `.venv/Scripts/python -m pytest tests/ -q` 全綠（基線 180）。
- README「API」表與「資料表」區塊各補一行。
- HTML 輸出一律 `esc()`；server 回傳含 `<mark>` 的字串時要先跳脫再插 mark（見 F8）。

## 批次 0 — 錨點（orchestrator 自己做，一個 commit，之後才能平行）
index.html：
- CSS：在 261 行 `/* ── 外觀主題` 之前插入 9 行錨點 `/* @F1-css */ … /* @F9-css */`，**每個錨點之間空 2 行**（git 需要未變動行才能無衝突合併相鄰 hunk）。
- HTML：552 行 `<section id="results" hidden></section>` 之後插 `<!-- @F9-html -->`；579 行 foldermodal 結尾 `</div>` 之後插 `<!-- @F7-html -->`。
- JS：990 行 `/* ── 控制項` 之前插 `/* @F1-js */ … /* @F9-js */`，同樣間隔 2 空行。
server.py：472 行 `if __name__` 之前插 `# @F1-api … # @F9-api`，間隔 2 空行。
規則：implementer 只在自己的錨點下方新增，錨點行保留。

## 建議順序與批次
| 批次 | 功能 | 可平行？ | index.html 既有函式觸點 |
|---|---|---|---|
| B1 | F5, F10, F11-spike | 是（都不碰 index.html；F5 改 indexer main/connect + server.db()，F10 只新增 cli.py，spike 只產 docs） | 無 |
| B2 | F1, F6, F8 | 是，函式互不重疊 | F1: loadHeat 956-972、.heat h3 546、card() bits 753-760、openProject 788；F6: openSession 834-837/872-875；F8: runSearch 909-948、hitList 640-647 |
| B3 | F2 → F3 → F4 → F7 | **否，依序**（都碰 openProject 或 card()/chips） | F2: openProject 794/795 之間；F3: openProject 798 之後；F4: card() 771、chips 541、passes 670-677；F7: card() 763/773、renderGrid 687、chips 542、openProject .actions 789-808、新 modal |
| B4 | F9, F11-impl | 是（F9 不准動 STATE 587-589，自己宣告 `let WEEK`；F11 只改 TOOL_LABEL 591 一行） | F9: header 529、show() 630、新 section |
依賴邊：F9→F1（api_call 表）、F11-impl→F11-spike、F10 若要 `--md` 輸出→F6 的 export.py（可選，缺檔則退回 JSON）。
server.py：F1 改 overview()/heatmap()、F8 改 search()、其餘只在錨點新增 → B2 三者不重疊。indexer.py：F1 改 index_transcript()/run()，F5 改 connect()/main，F11 改 run()：分屬不同批次。

---

## F1 Token / cost 統計
**已驗證事實**：主 transcript 的 `type:"assistant"` 每筆都有 `message.usage`（input_tokens / cache_creation_input_tokens / cache_read_input_tokens / output_tokens / output_tokens_details.thinking_tokens）、`message.model`、`requestId`。**同一個 API 呼叫會拆成多行 assistant（text 一行、tool_use 一行），usage 完全相同**：本機 3867 筆 → 1621 個 requestId。不去重會高估 2.4 倍。`model:"<synthetic>"` 是 CLI 合成的錯誤訊息，要跳過。`cost-state.modelUsage` 有 per-model 明細但每 session 只偶發一筆，不當主資料。
**資料**：schema.sql 新表
`api_call(id PK, session_id TEXT NOT NULL, project_id INT REFERENCES project(id), ts TEXT, request_id TEXT NOT NULL UNIQUE, model TEXT, input_tokens INT, cache_create_tokens INT, cache_read_tokens INT, output_tokens INT, thinking_tokens INT)` + `ix_call_project(project_id, ts)`、`ix_call_session(session_id)`。
- indexer.index_transcript()（255-330）在 `rtype == "assistant" and not isSidechain` 分支加：取 `usage = rec["message"].get("usage")`、`rid = rec.get("requestId") or rec.get("uuid")`；model 非 `<synthetic>` 且 usage 存在 → `INSERT OR IGNORE INTO api_call`。UNIQUE 負責同檔內去重與半行重讀。
- 舊 DB 回填：新函式 `backfill_usage(con)`，在 `run()` 的 index_transcript 迴圈後呼叫：若 `app_config` 無 `usage_backfilled` → 對每個 `session.transcript_path`（tool='claude'）從 offset 0 只讀 assistant 行寫 api_call，完成後寫 `usage_backfilled=1`。`--full` 會沿用 app_config（955-972 行邏輯）所以不會重跑，但 --full 本來就從 0 讀，正確。
- 子代理：`subagent` 表已有 input/output；migrate 補 `cache_create_tokens INT`、`cache_read_tokens INT`，index_subagent 444-446 一併加總（同樣要依 requestId 去重，用 set）。
**API**：
- `GET /api/heatmap?metric=prompts|tokens`（改 442-448）：tokens 時 `n = SUM(input+cache_create+output)`，另回 `out`、`cache_read` 欄位；day = `substr(ts,1,10)`。
- `GET /api/project/{id}/tokens` → `{total:{calls,input,cache_create,cache_read,output,thinking}, models:[{model,calls,output,input_all}] , by_day:[{day,output,input_all}], agents:{output,input}}`。
- overview()（99-116）加子查詢 `tokens_out`、`tokens_all`（=input+cache_create+output）；session_detail 加 `usage: {calls,…,models:[…]}`。
**UI**：546 行 `.heat h3` 內加 `<button class="chip" id="heatmetric" aria-pressed="false">tokens</button>`；loadHeat(metric) 讀 `STATE_HEAT`（自己的變數，不動 STATE）；cell title 改 `${day}：${n} prompts` / `${fmt(n)} tokens（out ${fmt(out)}）`。card() bits（753-760）push `p.tokens_out ? \`${fmt(p.tokens_out)} tok out\` : null`。openProject 788 行 `.note` 之後加 `<p class="note" id="models"></p>`，openProject 內 fetch `/api/project/{id}/tokens` 填「claude-opus-5 82% · claude-fable-5-1 18% · 1.2M out」。`fmt = n => n>=1e6 ? (n/1e6).toFixed(1)+"M" : n>=1e3 ? Math.round(n/1e3)+"k" : n`。
**測試** `tests/test_tokens.py`（新）：`test_usage_依_requestId_去重`（兩行同 requestId → 1 筆）、`test_synthetic_模型跳過`、`test_專案_token_聚合_進_overview`、`test_heatmap_metric_tokens`、`test_舊DB_會回填_usage`（indexed 後 `DELETE FROM api_call`、刪 app_config 旗標、`run(full=False)` → 有列且 turn 數不變）、`test_子代理_usage_去重`。
**驗收**：(1) 對 conftest 合成 transcript 加 2 行同 requestId，`api_call` 恰 1 筆；(2) `/api/heatmap?metric=tokens` 的 days 與 prompts 版 day 集合一致（有 prompt 的天）；(3) 舊 DB 增量後 `SELECT COUNT(*) FROM api_call > 0` 且 `turn` 數不變；(4) 卡片顯示 tok out；(5) 180+6 測試綠。
**風險**：cache_read 佔 97%（648M vs out 2.9M），拿來當熱度會把每天壓成同一色階 → 熱度用 input+cache_create+output，cache_read 只放 title。`uuid` 當 requestId 退路只在 requestId 缺席時（本機 1 筆）。回填讀 18 個檔約 25 MB，可接受；不要在 `sync_only()` 裡跑。

## F2 檔案檢視
**資料**：無新表（`file_touch(project_id, path, verb, via_agent, session_id, ts)` 已有 `ix_touch_project`）。
**API**：
- `GET /api/project/{id}/files?limit=60` → `{root, files:[{path, rel, n, edits, writes, agent_n, sessions, last_ts}]}`；`rel` = path 去掉 `project.real_path` 前綴（Windows 比對不分大小寫、`\`/`/` 都當分隔），去不掉就回原路徑。SQL：`GROUP BY path ORDER BY n DESC`。
- `GET /api/project/{id}/file?path=<原始 path>` → `{path, sessions:[{id, title, started_at, n, verbs, via_agent_n}]}`（JOIN session，`ORDER BY started_at DESC`）。path 用 query 參數（含反斜線，前端 encodeURIComponent）。404 若專案不存在；path 無資料回空 list 不報錯。
**UI**：openProject 模板 794（`.actions` 結尾 `</div>`）與 795（`.split`）之間插：
`<details id="filepanel" class="filepanel"><summary>改過的檔案 <span id="filecount"></span></summary><div class="tags" id="filelist"></div><div id="filesess"></div></details>`。openProject 內 fetch files → `#filelist` 用既有 `.tag` 顯示 `rel ×n`（`data-path`）；點 tag → fetch `/file` → `#filesess` 列 `.sess` 樣式的列（`data-id`），點列 → `openSession(id)` 並同步左欄 aria-current（openSession 已處理）。CSS 在 `/* @F2-css */`：`.filepanel{margin:0 0 .6em}` `.tag[aria-pressed="true"]{border-color:var(--accent);color:var(--ink)}`。
**測試** `tests/test_api.py` 追加：`test_專案檔案清單_依次數排序`、`test_檔案_rel_去掉專案前綴`、`test_檔案對應的_session`、`test_檔案查無資料回空`。conftest 只有 1 筆 file_touch，測試自己用 `jsonl()` 多寫幾筆再 `indexer.run(full=True)`。
**驗收**：(1) `/files` 第一筆是被改最多次的檔；(2) `rel` 不含 real_path；(3) `/file?path=` 回含該 session id；(4) 點 tag 後 `#filesess` 有 `.sess`；(5) 測試綠。
**風險**：子代理改的檔 `session_id` 是主線 session（index_subagent 用主線 id）→ 正確歸屬；`agent_n` 分開顯示即可。路徑大小寫：同一檔可能 `C:\` 與 `c:\` 兩種寫法，GROUP BY 前先 `lower()`（只在 Windows；`os.name == "nt"`）。

## F3 進度時間軸
**已驗證 bug**：`progress_signal` 的 `UNIQUE(session_id, kind, ts, body)` 對 `session_id IS NULL` 無效（SQLite NULL 互不相等），`memory_file` 每次索引都重插，本機每份 brain.md 已 5 份。時間軸會顯示重複，**必須先修**。
**資料**：indexer `_signal()`（354-360）在 `session_id is None` 時改成先 `SELECT 1 … WHERE session_id IS NULL AND kind=? AND ts=? AND body=?`，有就跳過。migrate 補一次性清理：`DELETE FROM progress_signal WHERE kind='memory_file' AND id NOT IN (SELECT MIN(id) … GROUP BY project_id, origin, ts, body)`（放在 migrate() 尾端，冪等）。
**API**：`GET /api/project/{id}/timeline?limit=200` → `{items:[{ts, kind, origin, session_id, title, goal, state, next_step}]}`，kind ∈ {away_summary, compact_summary, memory_file}（排除 last_prompt / cost_state），`ORDER BY ts ASC`，LEFT JOIN session 取 title。
**UI**：openProject 模板 798 行 `.split` 結尾 `</div>` 之後插 `<details id="timeline" class="tl" open><summary>目標演進 <span id="tlcount"></span></summary><ol id="tlist"></ol></details>`。每項：`<li data-s="…"><time>日期</time> <span class="src">origin</span> <b>目標</b> goal <b>狀態</b> state <b>下一步</b> next</li>`；有 session_id 者點擊 → openSession。同一天多筆合併日期標籤。CSS `/* @F3-css */`：`.tl ol{list-style:none;padding:0;margin:.4em 0;border-left:2px solid var(--line)}` `.tl li{padding:.2em .6em;font-size:.78rem}` `.tl time{color:var(--dim);font-size:.66rem}`。
**測試** `tests/test_indexer.py` 追加 `test_memory_file_不會因為_session_id_NULL_重複`（indexed 後再 `run(full=False)` 兩次 → memory_file 筆數不變；conftest 要加一個 `projects/slug-alpha/memory/brain.md`）、`test_migrate_會清掉既有的_memory_重複列`；`tests/test_api.py` 追加 `test_timeline_依時間遞增且排除_last_prompt`。
**驗收**：(1) 連跑三次增量後 memory_file 筆數 = 檔案數；(2) timeline 無 last_prompt/cost_state；(3) ts 遞增；(4) `#tlist` 有 li；(5) 測試綠。
**風險**：memory_file 的 ts 是檔案 mtime，只有內容變才會有新列 → 演進真的來自使用者改檔；`index_memory_files` 靠 `transcript_path LIKE slug%` 對回 project_id，slug 撞名（README 坑 2）時歸屬可能錯，維持現狀不擴大。

## F4 手寫 vs 自動 不一致偵測
**規則（全確定性）**：對每個專案取最新 `memory_file` 訊號（body 已存前 4000 字）：
1. `parser.memory_paths(text)`：抽路徑候選 —— 反引號內容、及 regex `[\w\-./\\~]+\.(py|js|ts|tsx|html|css|md|sql|json|toml|yaml|yml|sh|ps1)`；正規化（`\`→`/`、lower）。
2. 只保留「該專案 file_touch 歷史上出現過」的候選（用結尾比對：touch path 以 `/<候選>` 結尾），沒出現過的視為非專案檔，**不算**（避免 `~/.claude/plans/...` 誤報）。
3. 訊號 `mentioned_untouched`：候選在 30 天內沒有 file_touch，且 memory 的 goal/next_step 非空。訊號 `stale_memory`：memory ts 比該專案最後一筆 file_touch 早 14 天以上，且期間有 ≥10 筆 touch（手寫沒跟上實作）。
**資料**：無新表；純查詢時計算（43 專案、每個 1 次 body + 1 次 touch 查詢，便宜）。邏輯放 `server.mismatch(con, pid, real_path)` 回 `None | {"kind", "detail", "files":[…], "memory_ts"}`；path 抽取放 parser.py（純函式）。
**API**：overview 每個 project 加 `mismatch` 欄；`GET /api/project/{id}` 也加。
**UI**：card() 771 行 hotspots 之後加 `${p.mismatch ? \`<br><span class="flag">⚠ ${esc(p.mismatch.detail)}</span>\` : ""}`；chips 541 行 `onlyhist` 之後加 `<button class="chip" data-f="mismatch" aria-pressed="false">手寫與實作不符</button>`；passes()（670-677）加 `if (f.has("mismatch") && !p.mismatch) return false;`。detail 文案例：「brain.md 提到 server.py、parser.py，30 天內沒改過」「brain.md 已 21 天沒更新，期間改了 38 次檔」。
**測試** `tests/test_parser.py` 追加 `test_memory_paths_抽反引號與副檔名`、`test_memory_paths_忽略沒有副檔名的字`；`tests/test_api.py` 追加 `test_mismatch_提到的檔案沒動過`（合成 brain.md 提 config.py，file_touch ts 在 40 天前）、`test_mismatch_不存在的路徑不算`、`test_memory_跟上就沒有旗標`。
**驗收**：(1) 合成案例回 `mismatch.kind == "mentioned_untouched"`；(2) 提及未曾 touch 的路徑不產生旗標；(3) chip 篩選後只剩有 mismatch 的卡；(4) 沒 memory_file 的專案 `mismatch` 為 null；(5) 測試綠。
**風險**：中文 brain.md 常寫相對路徑或只寫檔名（本機樣本：`診所POS/docs/系統規劃.md`）→ 一律結尾比對。日期比較用 ISO 字串（file_touch.ts 是 UTC ISO，memory ts 也是 UTC ISO，可直接比）。不要把 `last_prompt` 當 memory。

## F5 背景蒸餾 `--watch`
**資料**：`indexer.connect()`（82-87）與 `server.db()`（32-35）都加 `con.execute("PRAGMA busy_timeout = 5000")`（WAL 已開）。無新表。
**行為**：indexer.py main（1026-1032）加 `--watch`、`--interval SECONDS`（預設 300，最小 30）、隱藏 `--max-runs N`（測試用）。新函式 `watch(interval, force_git=False, max_runs=None)`：迴圈 `run(full=False)` → 例外只印一行不退出（`sqlite3.OperationalError` 含 "locked" 時印「DB 忙，下一輪再試」）→ `time.sleep`；`KeyboardInterrupt` 乾淨退出 exit 0。`--watch` 與 `--full` 同時給 → argparse error（刪 DB 檔會讓正在跑的 server 抱著失效的 fd）。每輪只有真的有新增才印一行（run() 已回 stats，比對 prompt/turn 數）。`index_history()` 的每日備份 copy2 改成：目標存在且 size 相同就跳過。
**API**：無。`POST /api/reindex` 與 watcher 同時寫：busy_timeout 讓一方等最多 5 秒；兩邊都是短交易（run() 每個管線 commit 一次）。
**UI**：無。
**README**：新增「背景蒸餾」小節：(a) `python indexer.py --watch --interval 300`；(b) Windows 工作排程器：`schtasks /create /tn CLIHV-index /sc minute /mo 10 /tr "\"<repo>\.venv\Scripts\python.exe\" \"<repo>\indexer.py\""`（一次性執行、不用 --watch，排程器負責週期；環境變數 `PYTHONIOENCODING=utf-8`）；(c) cron：`*/10 * * * * cd <repo> && .venv/bin/python indexer.py >> index.log 2>&1`；(d) 與 server 並存的說明：WAL + busy_timeout，慢的一方等待、不會壞資料；`--full` 時要先關 server。
**測試** `tests/test_indexer.py` 追加：`test_watch_跑到_max_runs_就停`（`indexer.watch(interval=0, max_runs=2)` 回傳 2）、`test_watch_拒絕_full`（呼叫 main 的 argparse → `SystemExit`）、`test_busy_timeout_已設定`（`connect()` 後 `PRAGMA busy_timeout` == 5000）、`test_watch_遇到鎖不會退出`（monkeypatch `indexer.run` 第一次 raise OperationalError("database is locked")，第二次正常 → 回傳 2）。
**驗收**：(1) `--watch --max-runs 2 --interval 0` 正常結束；(2) `--watch --full` exit code 2；(3) server 開著時 watcher 連跑 3 輪不報錯；(4) README 有三種啟動方式；(5) 測試綠。
**風險**：`run()` 每輪都 `shutil.copy2` history.jsonl（219-221）→ 加 size 檢查；每輪都跑 `collect_git`（有指紋快取，OK）與 `scan_project_dirs`（stat 根目錄，OK）。Ctrl+C 在 sleep 中會丟 KeyboardInterrupt，要包在 try。

## F6 Session 匯出 Markdown
**資料**：無。新模組 `export.py`：純函式 `session_markdown(payload: dict) -> str`，payload 就是 `server.session_detail()` 的回傳（可被 F10 的 cli.py 直接重用）。
**格式**：`# <title or id8>` → 專案 real_path、期間、`claude --resume <id>`（codex 不印）→ `## 進度訊號`（goal/state/next_step + origin + 日期）→ `## Commits` → `## 改過的檔案`（path ×n，via_agent 標「子代理」）→ `## 指令`（依 kind 分組，每組最多 30 條，fenced code；指令內含 ``` 時改用 `~~~`）→ `## 對話`：每則 prompt 用 `> ` blockquote（逐行加前綴），其下一段 assistant 摘要（取 prompt.ts ≤ turn.ts < 下一則 prompt.ts 區間的最後一則 assistant_summary，與 index.html promptBlock 887-898 同邏輯）與工具 tag。slash prompt 用 `> _/cmd_`。子代理：`## 子代理` 列 description + result 前 1500 字。
**API**：`GET /api/session/{id}/export.md` → `fastapi.Response(content, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="session-<id 前 8 碼>.md"'})`；404 沿用 session_detail。
**UI**：openSession 模板 834-837 `.actions` 內加 `<button data-export="${esc(id)}">匯出 Markdown</button>`；872-875 的 onclick 加 `if (e.target.dataset.export) location.href = "/api/session/" + encodeURIComponent(e.target.dataset.export) + "/export.md";`。無新 CSS。
**測試** `tests/test_export.py`（新）：`test_匯出含_prompt_commit_與檔案`（用 `client` 打端點，斷言 `第一個問題`、`feat: 加上設定`、`config.py` 都在）、`test_content_type_與_attachment`、`test_不存在的_session_404`、`test_純函式不做_HTML_跳脫`（payload 帶 `<b>` 的 prompt → 原樣輸出）、`test_指令含反引號改用波浪線圍欄`。
**驗收**：(1) 端點回 200、`text/markdown`；(2) 輸出的 `## ` 段落順序如上；(3) prompt 每行都有 `> `；(4) 按鈕觸發下載；(5) 測試綠。
**風險**：檔名只用 ASCII（session id）避免 header 非 ASCII 問題；Windows 換行一律 `\n`（.gitattributes eol=lf）。`tools_json` 是 JSON 字串要 `json.loads`。

## F7 專案標籤 / 釘選 / 筆記
**資料**：schema.sql 新表 `project_meta(real_path TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '[]', note TEXT, updated_at TEXT)`。**以 real_path 為 key、不用 project_id**，所以 `--full` 後 id 重排也對得上。`indexer.run()`（952-966）的 `saved_config` 邏輯擴充：`--full` 前若舊 DB 有 `project_meta` 表則 `SELECT *` 抄出，新 DB 建好後 `INSERT OR REPLACE` 寫回（與 app_config 同段）。`scan_project_dirs` 的孤兒清除（767-782）不動：meta 沒有 FK，專案列被刪 meta 仍留著，路徑回來就接上。
**API**：
- `GET /api/project/{id}/meta` → `{pinned, tags:[…], note, updated_at}`（無列回預設值）。
- `PUT /api/project/{id}/meta` body 任意子集 `{pinned?: bool, tags?: [str], note?: str}` → 合併後回完整 meta；tags 去重、strip、丟空字串、上限 20、每個 ≤ 30 字；note ≤ 4000 字；400 若型別錯。
- `GET /api/tags` → `{tags:[{tag, n}]}`。
- overview() 的 SELECT（99-116）`LEFT JOIN project_meta m ON m.real_path = p.real_path` 加 `COALESCE(m.pinned,0) AS pinned, COALESCE(m.tags,'[]') AS tags, m.note`；Python 端 `json.loads(tags)`。
**UI**：card() 763 行 h2 的 `<span>` 內名稱前加 `${p.pinned ? "📌 " : ""}`；773 行 sources 之前加 `${p.tags.length ? \`<br>\${p.tags.map(t => \`<span class="tag">${esc(t)}</span>\`).join(" ")}\` : ""}${p.note ? \`<br><span class="src">📝 ${esc(p.note.split("\n")[0].slice(0,60))}</span>\` : ""}`。renderGrid 687 行排序後加穩定分割：`list = [...list.filter(p => p.pinned), ...list.filter(p => !p.pinned)]`。chips 542 行 `#feed` 之前加 `<select id="tagfilter" title="標籤"><option value="">全部標籤</option></select>`（loadOverview 後由 `/api/tags` 填），passes() 加 `if (TAG && !p.tags.includes(TAG)) return false;`（`let TAG = ""` 宣告在 `/* @F7-js */` 區）。編輯：openProject `.actions`（789-794）加 `<button data-act="meta">📌 / 🏷 / 📝</button>`，handler（804-808）加 `if (act === "meta") openMeta(project)`；`<!-- @F7-html -->` 處新增 `<div class="modal" id="metamodal" hidden><div class="sheet"><h2>專案標記</h2><label><input type="checkbox" id="metapin"> 釘選</label><input id="metatags" placeholder="標籤，逗號分隔"><textarea id="metanote" rows="6"></textarea><footer><button id="metasave">儲存</button><span class="grow"></span><button id="metaclose">關閉</button></footer></div></div>`；儲存後 `loadOverview()`。keydown Esc（1200 行）比照 foldermodal 先關 metamodal（這一行是 F7 唯一動到 1194-1204 的地方）。
**測試** `tests/test_meta.py`（新）：`test_meta_預設值`、`test_meta_部分更新與_tags_清洗`、`test_tags_出現在_overview_與_api_tags`、`test_full_重建保留_meta`（PUT → `indexer.run(full=True)` → GET 仍在）、`test_型別錯回_400`；`tests/test_web.py` 追加 `test_釘選會排前面`（RAW_JS 含 `p.pinned`）。
**驗收**：(1) PUT 後 GET 相同；(2) `--full` 後 meta 仍在；(3) overview 的 `tags` 是 list；(4) 釘選卡在任何排序下都在最前；(5) 測試綠。
**風險**：real_path 大小寫（Windows）：PUT 時用 project 表裡的 real_path 原字串當 key，不自行正規化。`#tagfilter` 與 chips 同列，640px 以下 chips 是 nowrap 橫捲，select 要 `flex:0 0 auto`（既有規則 `.chips > *` 已涵蓋）。

## F8 搜尋強化
**已驗證**：SQLite 3.50.4 的 trigram tokenizer 支援 `snippet()`/`highlight()`（實測含大小寫不敏感命中）。
**資料**：無。
**API**：`GET /api/search` 加參數 `since=YYYY-MM-DD`、`until=YYYY-MM-DD`（含當天：`ts < date(until,'+1 day')`）、`slash=all|only|exclude`（預設 all；only 時 reply/agent 範圍自動空）。search()（223-324）重構：`fetch()` 改成接受額外 `(where_sql, params)`，三組 SQL 各自加 `AND p.ts >= ? AND p.ts < ?` 與 `AND p.is_slash = ?`。每筆 hit 加 `snippet` 欄：
- FTS 路徑：SELECT 加 `snippet(prompt_fts, 0, char(2), char(3), '…', 24) AS snip`（turn_fts / subagent_fts 同理，subagent 用 result 欄位 index 1）；Python 端 `esc(snip)` 後把 `\x02`/`\x03` 換成 `<mark>`/`</mark>`（先跳脫再換，`<` 不會吃掉 mark）。
- LIKE 路徑（<3 字或 FTS 撲空）：Python 做：找 term 第一次出現位置（不分大小寫），取前後 60 字視窗，`esc()` 後用 `re.sub(re.escape(term), …, flags=I)` 包 `<mark>`。
- 回傳多加 `since, until, slash`。`mode` 不變。
**UI**：runSearch()（909-948）：scopebar（932-937）末尾加 `<input type="date" id="since"> <input type="date" id="until"> <button data-slash aria-pressed="${SLASH==="only"}">/ 指令</button>`；狀態放 `let SEARCH = {since:"", until:"", slash:"all"}`（`/* @F8-js */` 區，不動 STATE）；date 的 change 與 slash 的 click 都 `runSearch(STATE.lastRaw)`，重繪後把值填回。hitList()（640-647）：`h.snippet ? h.snippet : highlight(text, q)`（snippet 已是安全 HTML，不再 esc）。CSS 用既有 `mark`。
**測試** `tests/test_api.py` 追加：`test_搜尋_日期範圍`（since=2026-09-02 → 0 筆；since=2026-09-01 → ≥1）、`test_slash_only_只回_slash_prompt`、`test_slash_exclude`、`test_FTS_snippet_有_mark`、`test_LIKE_fallback_也有_snippet`（q=部署）、`test_snippet_已跳脫_HTML`（合成 prompt 含 `<b>x</b>`，snippet 含 `&lt;b&gt;`）。
**驗收**：(1) 新測試綠；(2) `mode` 在短查詢仍含 like；(3) 前端 hit 顯示 `<mark>`；(4) date/slash 變更會重新查詢並保留輸入值；(5) 既有 `test_兩字中文走_LIKE_fallback` 仍綠。
**風險**：`snippet()` 的 token 數對中文 trigram 是「字元 trigram 數」，24 大約 24 字，可接受。`date()` 對 `until=""` 要先在 Python 過濾，不要把空字串傳進 SQL。scopebar 在 640px 以下是 nowrap 橫捲，`input[type=date]` 要 `flex:0 0 auto`（既有規則涵蓋）。

## F9 每週回顧頁（依賴 F1）
**資料**：無新表（用 prompt / commit_ref / file_touch / api_call / progress_signal）。
**API**：`GET /api/week?start=YYYY-MM-DD` → `start` 預設本地時間本週一（`dt.date.today() - weekday`），`end = start+7`。回 `{start, end, projects:[{id, display_name, prompts, sessions, files, commits, tokens_out, tokens_all, goal, next_step}], commits:[{project_id, display_name, ts, message}], flags:[{project_id, display_name, path, n}] (同專案同檔 ≥3 次，本週內), tokens:{calls, output, input_all, cache_read}, prev:{prompts, tokens_out, commits}}`。所有 ts 比較用 ISO 字串 `>= start AND < end`（UTC；與 heatmap 一致）。projects 依 prompts DESC。
**UI**：header 529 行 `#reindex` 之前加 `<button id="week" title="每週回顧">📅 本週</button>`；`<!-- @F9-html -->` 處加 `<section id="weekly" hidden></section>`；show()（630）陣列加 `"weekly"`。`/* @F9-js */`：`let WEEK = null;`、`loadWeek(start)` 渲染：`.subbar`（← 回總覽、`◀ 上週`、`本週 ▶`、日期範圍）→ 四格統計（`.grid` 內用 `.card`：prompts、commits、tokens out、紅旗數，各附 vs 上週 ±%）→ `.grid` 專案卡（重用 card() 的 `.rows` 標記：目標／下一步）→ `<h3>commits</h3>` `.tag` 列 → `<h3>紅旗</h3>`。點專案卡 → openProject。Esc 回總覽已由既有 keydown 處理。CSS `/* @F9-css */`：`.kpi{font-size:1.4rem;font-weight:700}`。
**測試** `tests/test_week.py`（新）：`test_week_預設回本週一`（`start` 是週一）、`test_week_指定_start_含_prompt_與_commit`（start=2026-08-31 → conftest 資料在 09-01；projects 非空、commits 有 `feat: 加上設定`）、`test_week_空週回零`、`test_week_prev_對照`；`tests/test_web.py`：js id 自動掃會覆蓋 `#week`/`#weekly`。
**驗收**：(1) `/api/week?start=2026-08-31` 回 ≥1 專案與 1 commit；(2) 無參數時 start 為週一；(3) 頁面有 4 個 KPI 與專案卡；(4) ◀ ▶ 會改 start 重載；(5) 測試綠。
**風險**：prompt.ts 是 UTC，本地週一 00:00 ≠ UTC 週一，說明文字註明「以 UTC 計」。F1 未合併時 api_call 不存在 → 查詢包 try/except OperationalError 回 tokens 全 0（不要硬依賴表存在）。

## F10 終端機 CLI `cli.py`
**設計**：不走 HTTP、不開瀏覽器。直接呼叫 `server.overview()` / `server.search()` / `server.session_detail()` / `server.recent()`（FastAPI 端點就是普通函式，`Query(...)` 只是預設值；`server.search(q="x")` 可直接叫）。不改 server.py。
**指令**：`python cli.py search <q> [--scope all|prompt|reply|agent] [--limit 30] [--since] [--until] [--json]`；`python cli.py projects [--json]`；`python cli.py session <id> [--md] [--json]`（`--md` 用 F6 的 `export.session_markdown`，`ImportError` 時提示需先實作 F6）；`python cli.py recent [--limit 30]`。輸出格式（非 JSON）：每筆一行 `2026-09-01  alpha › 設定檔調整  第一個問題…`（text 截 120 字、換行壓成空格）；`--json` 印 `json.dumps(ensure_ascii=False, indent=2)`。exit code：無命中 1、DB 不存在 2（印「先跑 python indexer.py」）。第一行 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`。`since/until` 只在 F8 合併後生效（用 `inspect.signature(server.search)` 檢查參數存在，否則忽略並警告）。
**API / UI**：無。README「操作」表加 CLI 一行 + 「終端機」小節。
**測試** `tests/test_cli.py`（新，用 `indexed` fixture + `capsys`，`cli.main([...])` 回 exit code）：`test_cli_search_印出命中`、`test_cli_json_輸出可解析`、`test_cli_無結果_exit_1`、`test_cli_projects_列出專案`、`test_cli_session_不存在_exit_1`、`test_cli_utf8_在_ascii_stdout_不炸`（monkeypatch `sys.stdout` 為 `io.TextIOWrapper(BytesIO(), encoding="ascii", errors="replace")`）。
**驗收**：(1) `python cli.py search 第一個問題` 至少一行含 alpha；(2) `--json` 可 `json.loads`；(3) 無結果 exit 1；(4) 無 index.db exit 2；(5) 測試綠。
**風險**：`server` import 會觸發 `indexer` import 與 fastapi 載入（約 0.3s），可接受。DB_PATH 在 `indexer.DB_PATH`，測試靠 conftest monkeypatch，cli 不要自己快取路徑。

## F11 Gemini CLI —— 先 spike，再照 codex.py 模式實作
**本機已確認（唯讀 ls/cat `~/.gemini`）**：
- `~/.gemini/projects.json` = `{"projects": {"<real path 全小寫>": "<name>"}}`；本機兩筆（`c:\users\kaichih\desktop\project` → `project`，`c:\windows\system32` → `system32`）。
- `~/.gemini/tmp/<name>/.project_root` = 該專案真實路徑（**全小寫**）；`~/.gemini/tmp/<name>/chats/session-<YYYY-MM-DDTHH-MM>-<id 前 8 碼>.jsonl`；`~/.gemini/tmp/<name>/logs.json`（本機是 `[]`）；`~/.gemini/history/<name>/` 只有 `.project_root`。
- chats 檔第 1 行：`{"sessionId","projectHash","startTime","lastUpdated","kind":"main"}`；第 2 行：`{"$set":{"messages":[{"id","timestamp","type":"user","content":[{"text":"…"}]}],"lastUpdated":…}}`。第一則 user 是 CLI 注入的 `<session_context>…`（含整棵目錄樹），**不是真人 prompt** —— 與 Codex 的 role=="user" 注入是同一類陷阱。
**未知（不得臆測）**：assistant 訊息的 `type` 值；tool call / tool result 的表示法；後續訊息是 `$set` 整包覆寫還是 `$push` 追加（決定能不能用 offset 游標）；`logs.json` 的內容；`history/` 的用途。
**Spike 步驟（產出 `docs/gemini-schema.md`，不寫程式）**：
1. 在暫存目錄跑 `gemini`，送 2 則 prompt：一則純問答、一則要求建立並修改一個檔案（觸發工具）；`/quit`。
2. `cat ~/.gemini/tmp/<name>/chats/session-*.jsonl`：逐行記錄頂層 key（`$set`/`$push`/其他）、messages 內每種 `type` 的欄位、tool call 的 name/args/檔案路徑欄位、錯誤表示法、timestamp 格式。
3. `cat ~/.gemini/tmp/<name>/logs.json` 與 `~/.gemini/history/<name>/*`：判斷是否是 prompt 帳本（對應 Claude 的 history.jsonl）。
4. 再送一則 prompt 後 `--resume`，比對檔案是被覆寫還是追加（`wc -c` 前後 + 第 3 行是否存在）。
5. 寫成 docs/gemini-schema.md：檔案佈局、判別「真人 prompt」的規則、tool 事件正規化表（對齊 codex.event() 的 kind：meta/user/assistant/tool/tool_output）。
**實作（spike 後，另一位 implementer）**：新模組 `gemini.py`（`GEMINI_HOME = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home()/".gemini")`、`available()`、`session_files()`、`project_root_of(chat_path)`（讀同層 `.project_root`）、`event(rec)`），indexer 加 `index_gemini(con, resolver)` 在 `index_codex` 之後、`tool='gemini'`；`schema.sql` 的 tool 註解加 gemini；index.html 591 行 `TOOL_LABEL` 加 `gemini: "Gemini CLI"`。
**必守的坑**：(a) `.project_root` 是全小寫 → 先 `os.path.realpath()` 取回磁碟真實大小寫（存在時），不存在則用 `SELECT id FROM project WHERE real_path = ? COLLATE NOCASE` 對既有列，都沒有才建新列，否則同一專案會出現 `C:\…` 與 `c:\…` 兩張卡；(b) 若 spike 證實是 `$set` 整包覆寫 → 檔案**不是 append-only**，`scan_cursor` 的續讀邏輯不適用：mtime/size 變了就從 0 重讀，先 `DELETE` 該 session 的 prompt/turn/file_touch/command_run 再重插（比照 index_subagent 477-478）；(c) `<session_context>` 開頭的 user 訊息一律排除。
**測試**：`tests/test_gemini.py` 比照 test_codex.py 用合成檔（fixture 造 `projects.json`、`tmp/<name>/.project_root`、chats 檔）：`test_沒裝_gemini_整段跳過`、`test_session_context_不算_prompt`、`test_project_root_小寫也對回同一專案`、`test_覆寫式檔案重讀不重複`、`test_tool_事件抽出改檔`（欄位名依 spike 結果）。
**驗收**：(1) docs/gemini-schema.md 有實測樣本；(2) 無 `~/.gemini` 時索引輸出無 Gemini 字樣、測試綠；(3) 合成 session 的 `<session_context>` 不進 prompt 表；(4) 同專案不出現大小寫重複卡；(5) 兩次增量後筆數不變。
