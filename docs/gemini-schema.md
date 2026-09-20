# Gemini CLI session JSONL — 實測 schema 筆記

勘查日期：2026-09-20　CLI 版本：`@google/gemini-cli` **0.44.1**（npm 全域安裝）
樣本：本機既有 1 個 session 檔（2026-06-02）＋ spike 現跑出的 3 個 session 檔。

> ⚠ **本機沒有設定任何 Gemini 認證**（無 `settings.json`、無 `GEMINI_API_KEY`）。為了讓
> CLI 走完建檔流程，spike 用一把無效金鑰跑，每次都在 API 400 前寫好使用者那一半的檔案，
> **model 回覆與工具呼叫完全沒被觸發**。本文因此分兩種來源，寫 `gemini.py` 時務必分辨：
> **【實測】**＝真的在磁碟看到；**【源碼】**＝讀安裝的 bundle 寫檔程式碼
> （`bundle/chunk-LNSIEPG7.js`，內含原始 TS 檔名註解；chunk 檔名各安裝不同，引 TS 模組名較穩）。
> 兩者皆無者一律寫 **NOT OBSERVED**，不要自己補。

---

## 1. 檔案佈局

```
~/.gemini/
├── projects.json          {"projects": {"<真實路徑全小寫>": "<slug>"}}；slug 限 ^[a-z0-9-]+$
├── tmp/<slug>/
│   ├── .project_root      專案真實路徑，全小寫、**無結尾換行**（實測 55 bytes = 55 字元）
│   ├── chats/
│   │   ├── session-<YYYY-MM-DDTHH-MM>-<sessionId 前 8 碼>.jsonl   ← 主 session
│   │   └── <parentSessionId>/<sessionId>.jsonl                   ← subagent【源碼】NOT OBSERVED
│   ├── logs.json          prompt 帳本（見 §7）
│   └── logs/              空目錄；另有 memory/ checkpoints/ plans/ tracker/ 等本機未出現
└── history/<slug>/        shadow git repo（checkpoint 用，含 .gitconfig/.gitignore），
                           **不是** prompt 帳本。本機只有 .project_root。
```

檔名時間是 **UTC**（`toISOString().slice(0,16)`，`:`→`-`），與第 1 行 `startTime` 同源。
`chats/` 只在真的建 session 時才 mkdir：認證沒過就退出的那次只留下
`tmp/<slug>/.project_root` 與 `projects.json` 一筆，沒有 chats 目錄。

## 2. 一行 = 一筆 record，共 4 種

判別順序照 CLI 的 `loadConversationRecord`（`packages/core/src/services/chatRecordingService.ts`）：

| 判別式 | 種類 | 語意 |
|---|---|---|
| `$rewindTo` 是字串 | rewind | 刪掉該 id 起（含）之後**所有**訊息；找不到該 id 就清空全部 |
| `id` 是字串 | message | 依 id 新增或**覆寫**既有訊息 |
| `$set` 是物件 | metadata | 淺層合併進 metadata；`$set.messages` 在場時 → 先清空訊息表再整批重設 |
| 有 `sessionId` + `projectHash` | 檔頭 | 第 1 行；也會被當 metadata 合併 |

**`$push` 不存在**（全 bundle 0 命中）。訊息以裸 message record 逐行 append，讀檔必須「從頭依序重播」。

## 3. 各種 record 的逐字樣本（本次 spike，目錄樹已截）

**第 1 行 檔頭 metadata**【實測】
```json
{"sessionId":"63a95ada-876f-483a-bc98-099900d7ee46","projectHash":"7e7e4c22546469191410fc4c67feef18ad127598999c4e28e2d5c3e1bdd8b14a","startTime":"2026-09-20T10:18:01.708Z","lastUpdated":"2026-09-20T10:18:01.708Z","kind":"main"}
```
`kind`：`"main"` 【實測】／`"subagent"`【源碼】。`projectHash` = sha256(專案路徑)，**不等於** slug。

**第 2 行 `$set.messages` — CLI 注入的 `<session_context>`，不是真人**【實測】
```json
{"$set":{"messages":[{"id":"d04923d38bb0f6017037e74183378ef4","timestamp":"2026-09-20T10:18:01.710Z","type":"user","content":[{"text":"<session_context>\nThis is the Gemini CLI. We are setting up the context for our chat.\nToday's date is 2026年9月20日星期日 (formatted according to the user's locale).\nMy operating system is: win32\nThe project's temporary directory is: C:\\Users\\kaichih\\.gemini\\tmp\\gemini-spike\n- **Workspace Directories:**\n  - C:\\Users\\kaichih\\AppData\\Local\\Temp\\claude\\gemini-spike\n- **Directory Structure:**\n\nShowing up to 200 items (files + folders).\n\nC:\\Users\\kaichih\\AppData\\Local\\Temp\\claude\\gemini-spike\\\n└───before.txt\n\n\n</session_context>"}]}],"lastUpdated":"2026-09-20T10:18:01.710Z"}}
```

**第 3 行 真人 prompt（user message record）**【實測】
```json
{"id":"495eefe3-93e3-4359-bf5d-c25557c57f81","timestamp":"2026-09-20T10:18:01.875Z","type":"user","content":[{"text":"Reply with exactly: pong"}]}
```

**第 4 / 5 行 純 metadata 更新**【實測】
```json
{"$set":{"lastUpdated":"2026-09-20T10:18:01.875Z"}}
{"$set":{"sessionId":"63a95ada-876f-483a-bc98-099900d7ee46"}}
```

**assistant（`type:"gemini"`）── NOT OBSERVED**（認證不過，模型沒回過話）。
**tool call ── NOT OBSERVED**。兩者的欄位見 §5，只有源碼依據。

## 4. message record 欄位

| 欄位 | 值 | 來源 |
|---|---|---|
| `id` | UUIDv4；或 `deriveStableId()` 的 32 碼 hex（如 session_context） | 實測 |
| `timestamp` | ISO-8601 **UTC 毫秒** `"2026-09-20T10:18:01.875Z"`，同檔內單調不減 | 實測 |
| `type` | `"user"` 實測；`"gemini"`（= assistant）、`"info"`（合成訊息）源碼 | 混 |
| `content` | **string 或 Part[]**（`[{"text":…}]`）。user 實測是 Part[]；`recordMessage` 寫 `"gemini"` 時給的是純字串 → 兩種都要吃 | 混 |
| `displayContent` | 可選，UI 用 | 源碼 |
| `thoughts` | 僅 `type:"gemini"`：`[{subject, description, timestamp}]`（`**粗體**` 前段當 subject） | 源碼 |
| `tokens` | 僅 `type:"gemini"`：`{input, output, cached, thoughts, tool, total}` | 源碼 |
| `model` | 僅 `type:"gemini"`：模型名字串 | 源碼 |
| `toolCalls` | 僅 `type:"gemini"`：見 §5 | 源碼 |

檔頭 metadata 另有 `summary` / `memoryScratchpad` / `directories`（subagent 才寫）── 皆 NOT OBSERVED。

## 5. 真人 prompt 判別 ★最重要

`<session_context>` 那一則的 `id` 是**固定常數**：`sha256("environment-context")[:32]`
= `d04923d38bb0f6017037e74183378ef4`（本機兩個不同日期的 session 檔都是這個值，已用
`node -e` 重算驗證）。但那是 0.44.1 的實作細節，文字前綴才是保證，兩個都擋：

```python
SESSION_CONTEXT_ID = "d04923d38bb0f6017037e74183378ef4"  # sha256("environment-context")[:32]

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""

def is_real_user_prompt(msg):
    if msg.get("type") != "user":
        return False
    if msg.get("id") == SESSION_CONTEXT_ID:       # CLI 注入的環境 context
        return False
    text = text_of(msg.get("content")).lstrip()
    if text.startswith("<session_context>"):      # 版本換了 id 也擋得住
        return False
    return bool(text.strip())
```

`type=="user"` 也會被 `recordSyntheticMessage("user", …)` 寫出合成訊息（工具回應回填、binary
注入），**光看 type 會把注入內容當成人在打字** —— 與 Codex `role=="user"` 是同一類陷阱。

## 6. tool call 表示法【全部源碼，NOT OBSERVED】

工具**不是**獨立一行，而是掛在 `type:"gemini"` 訊息的 `toolCalls` 陣列裡；同一筆會隨
狀態變化被**整則重寫**（同 id append 一次新的 message record）。
`recordCompletedToolCalls` + `recordToolCalls` 組出的每個元素：

| 欄位 | 內容 |
|---|---|
| `id` | callId |
| `name` | 工具名（`originalRequestName ?? name`） |
| `args` | 參數物件 —— **檔案路徑欄位名 NOT OBSERVED**（Gemini 內建工具慣例是 `file_path` / `absolute_path`，未驗證，不要照抄） |
| `result` | `responseParts`（Part[]）或 `null` |
| `status` | `validating`｜`scheduled`｜`executing`｜`awaiting_approval`｜`success`｜`error`｜`cancelled` |
| `timestamp` | ISO-8601 UTC |
| `resultDisplay` | 字串或物件（改檔工具是 `{diffStat:…}` 之類） |
| `description` / `displayName` / `renderOutputAsMarkdown` | 由 toolRegistry 補上的顯示用欄位 |
| `agentId` | 有子代理時才有 |

錯誤 = `status == "error"`；工具輸出上限 `MAX_TOOL_OUTPUT_SIZE = 50 KiB`。

## 7. logs.json

`[{sessionId, messageId, timestamp, type, message}]` 的 JSON **陣列**，`type` 目前只有 `"user"`
（`MessageSenderType.USER`）→ 等價於 Claude 的 `history.jsonl`。**整檔 rewrite（`writeFile`），不是 append。**
【實測】本機 `tmp/system32/logs.json` 是 `[]`，spike 的 headless 跑法根本沒建這個檔
→ 內容非空的樣本 **NOT OBSERVED**，當成 optional 處理。

## 8. append-only 判定：**是 append-only**

`ChatRecordingService.appendRecord()` 唯一的寫法是 `fs.appendFileSync`（無 truncate、
無 rename、無 rewrite）。實測佐證（同一個檔，中間跑了一次 `--resume latest -p …`）：

| | bytes | lines |
|---|---|---|
| 之前 | 1189 | 4 |
| 之後 | 1455 | 7 |

且前 227 bytes（第 1 行）逐字 `diff` 相同 → 只在尾端長。

**但 append-only ≠ 可以只讀新行。** `$set.messages` 會清空訊息表整批重設、`$rewindTo` 會刪尾巴。
**對 cursor 的結論**：size/mtime 變了就**從 offset 0 整檔重播**，先 `DELETE` 該 session 的
prompt/turn/file_touch/command_run 再重插（比照 `index_subagent`）。byte offset 只適合判斷「有沒有變」。

### ⚠ `--resume` 會留下孤兒檔，且 sessionId 會重複
`--resume latest` 那次實測產生**兩個**檔：CLI 開機時先照常新建
`session-2026-09-20T10-19-63a95ada.jsonl`（只有檔頭 + `<session_context>` 兩行，永遠沒有真人訊息），
接著才切到舊檔續寫，而**兩個檔的 `sessionId` 一模一樣**（`63a95ada-876f-483a-bc98-099900d7ee46`）。
→ (a) DB 的 key 用**檔名/路徑**，不要用 `sessionId`；(b) 重播後沒有任何「真人 prompt 或 gemini
訊息」的檔案整個跳過（CLI 的 `--list-sessions` 也是用 `hasUserOrAssistantMessage` 濾掉孤兒檔）。

## 9. 正規化對照表（對齊 `codex.event()` 的 kind）

| Gemini record | kind | 取值 |
|---|---|---|
| 第 1 行檔頭 | `meta` | `session_id`←`sessionId`、`ts`←`startTime`、`cwd`←同層 `.project_root`（**全小寫**） |
| `$set.messages` | 展開成底下各 message，**先清空**已累積的訊息 | — |
| `$rewindTo` | 刪掉該 id（含）之後的訊息 | — |
| message `type:"user"` 且過 §5 判別 | `user` | `text`←`text_of(content)`、`ts`←`timestamp` |
| message `type:"gemini"`、有文字 | `assistant` | `text`←`content`（字串或 Part[]） |
| message `type:"gemini"` 的 `toolCalls[i]` | `tool` | `name`←`name`、`args`←`args`、`call_id`←`id` |
| 同上、`result`/`resultDisplay` | `tool_output` | `text`←`result` 攤平、`has_error`←`status=="error"` |
| message `type:"gemini"` 的 `thoughts[i]` | `reasoning` | `text`←`subject + description` |
| message `type:"gemini"` 的 `tokens` | `tokens` | `input`←`tokens.input`、`output`←`tokens.output` |
| `$set`（無 `messages`）／未過 §5 判別的 user／`type:"info"` | 忽略 | — |

## 10. NOT OBSERVED 清單（不得臆測，要嘛實作時防禦性處理，要嘛等下次有認證再測）

1. `type:"gemini"` 的訊息**一則都沒看過** —— 含 `content` 是字串還是 Part[]、`model` 實際值。
2. `toolCalls` 整段：**工具名、args 的檔案路徑欄位名、`result` 形狀、錯誤長相**。
3. `type:"info"` 合成訊息的內容；`$rewindTo` record（要 `/rewind` 才會出現）。
4. subagent 檔 `chats/<parentSessionId>/<sessionId>.jsonl`、`kind:"subagent"`、`directories`。
5. `summary`、`memoryScratchpad`、`memoryScratchpadIsStale`、非空的 `logs.json`。
6. 舊格式 `.json` session 檔（loader 有 `parseLegacyRecordFallback`：整檔一個 JSON 物件含 `messages` 陣列）。
7. `history/<slug>/` 裡 shadow git repo 的實際內容（本機只有 `.project_root`）。

## 11. 給 implementer 的坑

- `.project_root` 全小寫 → 先 `os.path.realpath()` 取回真實大小寫；取不到就 `SELECT id FROM project WHERE real_path = ? COLLATE NOCASE`，都沒有才建新列。
- 檔名時間是 **UTC**，不是本地時間。`content` 兩種型別都要吃，`text_of()` 對非 dict 元素要容錯。
- 除 `id` / `type` / `timestamp` 外一律 `dict.get(key, default)` 保守存取。
