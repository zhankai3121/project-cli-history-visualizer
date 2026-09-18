# Claude Code session JSONL — 實測 schema 筆記

勘查日期：2026-09-17
樣本版本：CLI `2.1.231 / 238 / 258 / 261 / 263 / 270 / 274`
樣本來源：三個專案的 transcript —— 一個小檔、一個 9.7 MB / 1917 行的大檔、一個含 compact 實例的中型檔。

> 這份是 `parser.py` 的規格來源。任何判別式改動都要先回來對照這裡。

---

## 1. 頂層 `type` — 實測 14 種

分兩類：

**訊息樹記錄**（有 `uuid` / `parentUuid`，串成對話鏈）
`user` · `assistant` · `attachment` · `system`

**側車狀態記錄**（無 `uuid`，多數無 `timestamp`，只用 `sessionId` 當 key，隨 session 進行被反覆追加新行代表「當下狀態」。**只在主 session 檔出現，subagent 檔完全沒有**）
`last-prompt` · `mode` · `permission-mode` · `atis-latch` · `ai-title` · `cost-state` · `queue-operation` · `bridge-session` · `file-history-snapshot` · `file-history-delta`

大檔實測分布：
```
assistant 477 | attachment 331 | user 308 | queue-operation 124 | last-prompt 98
bridge-session 98 | mode 97 | permission-mode 97 | atis-latch 97 | ai-title 96
system 44 | file-history-delta 39 | file-history-snapshot 11
```

### ⚠️ `type:"summary"` 不存在

整個 `~/.claude/projects` 樹 grep **0 筆命中**。網路上/舊認知常提到的 `{"type":"summary","summary":…,"leafUuid":…}` 在這些版本已被拆成別的機制（見 §4）。不要寫任何依賴它的程式碼。

---

## 2. 判別「真人 prompt」

最強單一訊號：**`origin.kind == "human"`**。在 1160 筆 `user` 記錄的大樣本中與其餘所有訊號 bijective。

```python
def is_real_user_prompt(rec: dict) -> bool:
    """True 僅當這是使用者真的打字 / 排隊送出 / 點擊建議按鈕的內容。"""
    if rec.get("type") != "user":
        return False
    if rec.get("isSidechain"):        # subagent 分支
        return False
    if rec.get("isMeta"):             # Skill 展開等合成的同輪伴隨訊息
        return False
    if rec.get("isCompactSummary"):   # compact 續接摘要 stub
        return False
    if (rec.get("origin") or {}).get("kind") != "human":
        return False
    if rec.get("promptSource") not in ("typed", "queued", "suggestion_accepted"):
        return False
    return isinstance(rec.get("message", {}).get("content"), str)


def is_tool_result_echo(rec: dict) -> bool:
    if rec.get("type") != "user":
        return False
    c = rec.get("message", {}).get("content")
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c
    )
```

實測交叉表 `(origin.kind, content 型別, promptSource)`：

| 組合 | 筆數 | 意義 |
|---|---|---|
| `(None, list, tool_result)` | 253 | tool_result 回填 |
| `(task-notification, str, 'system')` | 34 | 背景任務完成通知（自動送出的「user」回合） |
| `(None, str, None)` | 9 | 各種合成 stub（slash 展開、local-command-stdout） |
| `(human, str, 'typed')` | 8 | **真人打字** |
| `(human, str, 'suggestion_accepted')` | 1 | **真人點擊建議** |
| `(None, list, text)` | 3 | Skill 展開注入（`isMeta:true`） |

### Hook 注入不在 `type:"user"` 裡

對全樣本的 `type:"user"` payload 搜 `system-reminder`，**命中率恆為 0**。hook 一律是獨立的 `type:"attachment"`：

```json
{"type":"attachment","attachment":{"type":"hook_additional_context",
 "hookName":"UserPromptSubmit","content":["CAVEMAN MODE ACTIVE…"]}}
```

→ 所以 history.jsonl 的 `display` 乾淨、transcript 的真人 prompt 也乾淨。不需要寫清洗正規式。

### slash command 展開

出現在 `type:"user"` 但 `origin` / `promptSource` 都缺席，上面的判別式天然排除：

```json
{"message":{"content":"<command-name>/model</command-name>\n<command-message>model</command-message>…"}}
{"message":{"content":"<local-command-stdout>Set model to Opus 5…</local-command-stdout>"}}
```

保險起見再加一條：`content` 以 `<command-name>` 開頭者一律非真人。

---

## 3. 進度訊號金礦（本專案最重要的一節）

### 3.1 `away_summary` — AI 自己寫的進度摘要 ★最高價值

```json
{"type":"system","subtype":"away_summary",
 "content":"Goal: ship the export feature… Next: load the build and test it."}
```

閒置時自動產生。格式含 `Goal:` / `Next:` → 直接正規式拆成兩欄，就是「目標 / 下一步」。

### 3.2 `isCompactSummary` — 結構化的整段 session 摘要 ★次高

```json
{"type":"user","isCompactSummary":true,"isVisibleInTranscriptOnly":true,
 "message":{"content":"This session is being continued from a previous conversation that ran out of context. Summary:\n1. Primary Request and Intent:…"}}
```

內含編號章節（Primary Request and Intent / Key Technical Concepts / Errors and fixes / Pending Tasks / Current Work / Next Step…）。抽「Primary Request and Intent」當目標、「Pending Tasks」+「Next Step」當下一步。

### 3.3 `ai-title` — session 標題

```json
{"type":"ai-title","aiTitle":"HTML 檔案整合","sessionId":"…"}
```

側車、會被後續行覆寫 → **取該 session 檔內最後一筆**。

### 3.4 `last-prompt` — 最後一則真人 prompt（含 `leafUuid`）

```json
{"type":"last-prompt","leafUuid":"<uuid>","lastPrompt":"<使用者最近一則 prompt 的截斷原文>…","sessionId":"…"}
```

`leafUuid` = 當前鏈葉節點，**不是**摘要索引。`lastPrompt` 直接給截斷原文，是現成的「最後在忙什麼」。

### 3.5 `cost-state` — 規模量化（optional，非每個 session 都有）

```json
{"type":"cost-state","totalCostUSD":0,"totalAPIDuration":0,"totalToolDuration":0,
 "totalLinesAdded":0,"totalLinesRemoved":0,"totalDuration":0,"startTime":…,"modelUsage":{}}
```

`totalLinesAdded` / `totalLinesRemoved` 是最直接的「做了多少事」。任何版本都要當 optional 處理。

### 3.6 `compact_boundary` — 壓縮事件

```json
{"type":"system","subtype":"compact_boundary","logicalParentUuid":"df1e0389-…",
 "compactMetadata":{"trigger":"manual","preTokens":139314,"postTokens":14129,"cumulativeDroppedTokens":125185}}
```

`parentUuid` 為 `null` 但 `logicalParentUuid` 保留真實鏈 → 接鏈時要 fallback 到它。

---

## 4. 時間戳 / token / cost

- 時間戳一律 `"2026-09-17T06:03:27.026Z"`：ISO-8601、**UTC**、毫秒。同檔內單調不減。
- `mode` / `permission-mode` / `atis-latch` / `ai-title` / `last-prompt` / `bridge-session` **沒有 `timestamp` 欄位**。
- `type:"user"` **永遠沒有** token / cost 欄位。
- `type:"assistant"` 的 `message.usage`：`input_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` / `output_tokens` / `output_tokens_details.thinking_tokens` / `service_tier` / `cache_creation{ephemeral_1h_input_tokens, ephemeral_5m_input_tokens}` …，**無逐則 costUSD**。

---

## 5. 子代理連結

```
主檔 assistant → message.content[] → {"type":"tool_use","name":"Agent","id":"toolu_<id>"}
                                                                          │
subagents/agent-<agentId>.meta.json → {"toolUseId":"toolu_<id>", …}  ←─┘
subagents/agent-<agentId>.jsonl     → 每行 sessionId 與主檔【相同】+ agentId + isSidechain:true
```

`meta.json` 欄位：`agentType` / `description` / `toolUseId` / `spawnDepth` / `requestShape` / `requestNonInteractive` / `model`。

子代理檔只有 `user` / `assistant` / `attachment` 三種 type，無側車類型。其第一筆（orchestrator 交辦內容）`parentUuid:null`、content 是字串、但 **`origin` 與 `promptSource` 兩個 key 完全缺席** → §2 判別式正確判為非真人。

---

## 6. resume / compact 都寫回同一個檔

- 大檔（1917 行）單檔內同時有 `version` `2.1.270` 與 `2.1.274` → 跨版本升級後 `--resume` 仍寫回同檔。
- 另一個 1200+ 行的檔案第 305 行是 `compact_boundary`，前後皆為連續一般記錄 → `/compact` 不另開檔。
- 無專屬的「進程重啟」記錄。替代訊號：`attachment.type=="hook_success"` 且 `hookName=="SessionStart:startup"`（每次 CLI 啟動含 resume 各一次），或相鄰記錄的 `version` 變化。

### ⚠️ `parentUuid` 可能無解

某個檔案第一筆真人 prompt 的 `parentUuid` 在同專案所有檔案 grep = 0。
→ parser **不可假設 `parentUuid` 必能解析**，查無父節點時優雅降級為根節點。

---

## 7. 版本不穩定欄位

| 欄位 | 情況 |
|---|---|
| `atis-latch` | 2.1.231 完全沒有；2.1.238 起穩定 |
| `queue-operation` / `bridge-session` | 2.1.258 起出現（也可能是行為訊號而非版本訊號） |
| `cost-state` | 各版本皆僅偶發 1 次 → 永遠 optional |
| `sessionKind:"bg"` | 只在 2.1.263 一筆看到 |
| `assistant` 的 `slug` / `wireToolInputs` / `apiBlockIndex` / `attributionSkill` / `apiErrorStatus` / `isApiErrorMessage` / `quotaLimits` / `perTurnEffort` | 只在 2.1.270+ |

**結論**：除 `type` / `message` / `timestamp` / `sessionId` / `version` 外，一律 `dict.get(key, default)` 保守存取。
