"""Google Gemini CLI 的 session 解析。

格式來源（2026-09-20 勘查 `@google/gemini-cli` 0.44.1）：完整筆記在
`docs/gemini-schema.md`，改這個檔之前先讀那一份。

檔案佈局：
  ~/.gemini/projects.json               {"projects": {"<真實路徑全小寫>": "<slug>"}}
  ~/.gemini/tmp/<slug>/.project_root    專案真實路徑，**全小寫**、無結尾換行
  ~/.gemini/tmp/<slug>/chats/session-<YYYY-MM-DDTHH-MM>-<id 前 8 碼>.jsonl
  ~/.gemini/tmp/<slug>/chats/<parentSessionId>/<sessionId>.jsonl   子代理【未實測】

三條必守的事實：

⚠ 1. 真人打的字**不是**「第一則 type=="user"」。CLI 會先注入一則
     `<session_context>`（含整棵目錄樹）當開場白，`type` 也是 `"user"`。
     光看 type 會把環境資訊當成使用者 prompt —— 跟 Codex 的 `role=="user"`
     是同一類陷阱。判別式在 `is_real_user_prompt()`。

⚠ 2. 檔案是 append-only（只有 `fs.appendFileSync`），**但不能只讀新行**：
     `$set.messages` 會清空訊息表整批重設，`$rewindTo` 會砍掉尾巴。
     所以只能整檔從頭 `replay()`，游標僅用來判斷「有沒有變」。

⚠ 3. `--resume` 會另外開一個只有檔頭的孤兒檔，而且**兩個檔的 sessionId 一模一樣**。
     資料庫的 key 因此要混進檔案路徑（`session_id_of()`），不能只用 sessionId。

本機勘查時沒有 Gemini 認證，`type:"gemini"` 的訊息與 `toolCalls` 一則都沒看過
（docs 的 NOT OBSERVED 清單）。這裡對那些形狀一律保守存取：`content` 吃字串也吃
Part[]、缺欄位用預設值、認不得的形狀回空清單，絕不拋例外。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

GEMINI_HOME = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home() / ".gemini")

# CLI 注入的那則環境 context，id 是 sha256("environment-context")[:32]（0.44.1 實測）。
# id 是實作細節，換版本可能變；文字前綴才是保證 —— 兩個都擋。
SESSION_CONTEXT_ID = "d04923d38bb0f6017037e74183378ef4"
SESSION_CONTEXT_PREFIX = "<session_context>"

# 工具名與 args 的欄位名都是 NOT OBSERVED（docs/gemini-schema.md §6、§10）。
# 寧可多認幾個別名，也不要漏掉改檔紀錄 —— 認錯的代價只是少一列，硬猜死一種才是真的壞。
_FILE_TOOLS = {"write_file": "Write", "create_file": "Write", "delete_file": "Delete",
               "replace": "Edit", "edit": "Edit", "edit_file": "Edit",
               "smart_edit": "Edit", "str_replace": "Edit"}
_PATH_KEYS = ("file_path", "absolute_path", "path", "filePath", "filename", "file")
_SHELL_TOOLS = {"run_shell_command", "shell", "bash", "run_command",
                "execute_command", "terminal"}
_COMMAND_KEYS = ("command", "cmd", "script")


# ── 發現 session 檔 ───────────────────────────────────────────────────────

def available():
    return (GEMINI_HOME / "tmp").is_dir()


def session_files():
    """tmp/<slug>/chats/ 底下所有 jsonl（含子代理那層子目錄）。"""
    root = GEMINI_HOME / "tmp"
    if not root.is_dir():
        return []
    try:
        slugs = sorted(root.iterdir())
    except OSError:
        return []
    found = []
    for slug in slugs:
        chats = slug / "chats"
        if not chats.is_dir():
            continue
        try:
            found.extend(p for p in sorted(chats.rglob("*.jsonl")) if p.is_file())
        except OSError:
            continue
    return found


def project_root_of(path):
    """session 檔 -> 同一個 tmp/<slug>/ 底下 `.project_root` 的內容。

    實測那個檔是**全小寫、無結尾換行**的真實路徑，呼叫端要自己還原大小寫。
    """
    marker = None
    for parent in path.parents:
        if parent.name == "chats":
            marker = parent.parent / ".project_root"
            break
    if marker is None:
        marker = path.parent.parent / ".project_root"
    try:
        return marker.read_text(encoding="utf-8", errors="replace").strip() or None
    except OSError:
        return None


def session_id_of(path, session_id=None):
    """資料庫用的 session key。

    `--resume` 會讓兩個不同檔案帶同一個 sessionId，所以一律混進檔案路徑的短雜湊，
    否則兩個檔會互相覆寫。雜湊取相對 GEMINI_HOME 的路徑，家目錄整個搬走才會變。
    """
    try:
        rel = path.relative_to(GEMINI_HOME)
    except ValueError:
        rel = path
    seed = str(rel).replace("\\", "/").lower()
    short = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    base = session_id if isinstance(session_id, str) and session_id else path.stem
    return f"{base}-{short}"


# ── 訊息判別 ──────────────────────────────────────────────────────────────

def _items(value):
    return value if isinstance(value, list) else []


def text_of(content):
    """`content` 是純字串或 Part[]（`[{"text": …}]`），兩種都要吃。"""
    if isinstance(content, str):
        return content
    parts = []
    for block in _items(content):
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _flat(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def is_real_user_prompt(msg):
    """真人打的字 —— 整個 adapter 最重要的一條防線。

    `type=="user"` 還包含 CLI 注入的 `<session_context>` 與
    `recordSyntheticMessage("user", …)` 寫出的合成訊息，都不是人在打字。
    """
    if not isinstance(msg, dict) or msg.get("type") != "user":
        return False
    if msg.get("id") == SESSION_CONTEXT_ID:
        return False
    text = text_of(msg.get("content")).lstrip()
    if text.startswith(SESSION_CONTEXT_PREFIX):
        return False
    return bool(text.strip())


def worth_indexing(messages):
    """整份檔案值不值得寫進資料庫。

    `--resume` 留下的孤兒檔只有檔頭 + `<session_context>`，永遠不會有真人訊息。
    CLI 自己的 `--list-sessions` 也是用「有沒有 user 或 assistant 訊息」濾掉它們。
    """
    return any(is_real_user_prompt(m)
               or (isinstance(m, dict) and m.get("type") == "gemini")
               for m in messages)


# ── 整檔重播 ──────────────────────────────────────────────────────────────

def replay(records):
    """逐行重播 -> (metadata, 最終訊息串)。

    判別順序照 CLI 的 `loadConversationRecord`：`$rewindTo` -> `id` -> `$set` -> 檔頭。
    **不能只讀新行** —— `$set.messages` 會整批重設、`$rewindTo` 會砍尾巴。
    """
    meta, messages = {}, []
    for rec in records:
        if not isinstance(rec, dict):
            continue

        rewind = rec.get("$rewindTo")
        if isinstance(rewind, str):
            # 刪掉該 id 起（含）之後所有訊息；找不到該 id 就清空全部
            cut = next((i for i, m in enumerate(messages) if m.get("id") == rewind), None)
            messages = messages[:cut] if cut is not None else []
            continue

        if isinstance(rec.get("id"), str):
            # 同一則工具呼叫會隨狀態變化被整則重寫（同 id 再 append 一次）
            for i, older in enumerate(messages):
                if older.get("id") == rec["id"]:
                    messages[i] = rec
                    break
            else:
                messages.append(rec)
            continue

        changes = rec.get("$set")
        if isinstance(changes, dict):
            if "messages" in changes:
                messages = [m for m in _items(changes.get("messages"))
                            if isinstance(m, dict)]
            meta.update({k: v for k, v in changes.items() if k != "messages"})
            continue

        if rec.get("sessionId") or rec.get("projectHash"):
            meta.update(rec)
    return meta, messages


# ── 正規化（kind 與 codex.event() 對齊）──────────────────────────────────

def events(msg):
    """一則 message record -> 正規化事件清單。

    回傳的是**清單**而不是單一事件（codex 那邊是一行一事件）：一則
    `type:"gemini"` 訊息可以同時帶思考、數個工具呼叫、回覆文字與 token 統計。

    順序刻意讓 tool_output 排在 assistant 之前 —— 同一則訊息裡的工具錯誤
    要算在這一回合，不是下一回合。
    """
    if not isinstance(msg, dict):
        return []
    ts = msg.get("timestamp")
    kind = msg.get("type")

    if kind == "user":
        if not is_real_user_prompt(msg):
            return []
        return [{"kind": "user", "ts": ts, "text": text_of(msg.get("content")).strip()}]

    if kind != "gemini":
        return []                    # type:"info" 之類的合成訊息不進資料庫

    out = []
    for thought in _items(msg.get("thoughts")):
        if not isinstance(thought, dict):
            continue
        text = " ".join(str(thought.get(k) or "") for k in ("subject", "description")).strip()
        if text:
            out.append({"kind": "reasoning", "ts": thought.get("timestamp") or ts,
                        "text": text})

    for call in _items(msg.get("toolCalls")):
        if not isinstance(call, dict):
            continue
        call_ts = call.get("timestamp") or ts
        args = call.get("args")
        out.append({"kind": "tool", "ts": call_ts,
                    "name": str(call.get("name") or ""),
                    "args": args if isinstance(args, dict) else {},
                    "call_id": call.get("id")})
        status = call.get("status")
        if status is not None or call.get("result") is not None \
                or call.get("resultDisplay") is not None:
            out.append({"kind": "tool_output", "ts": call_ts,
                        "text": text_of(call.get("result")) or _flat(call.get("resultDisplay")),
                        "has_error": status == "error"})

    text = text_of(msg.get("content")).strip()
    if text:
        out.append({"kind": "assistant", "ts": ts, "text": text})

    tokens = msg.get("tokens")
    if isinstance(tokens, dict):
        out.append({"kind": "tokens", "ts": ts,
                    "input": tokens.get("input"), "output": tokens.get("output")})
    return out


# ── 工具 ──────────────────────────────────────────────────────────────────

def is_file_tool(name):
    return str(name or "").lower() in _FILE_TOOLS


def file_verb(name):
    return _FILE_TOOLS.get(str(name or "").lower(), "Edit")


def file_target(args):
    """改檔工具的參數 -> 檔案路徑。欄位名 NOT OBSERVED，所以逐個別名試。"""
    if not isinstance(args, dict):
        return ""
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def is_shell_tool(name):
    return str(name or "").lower() in _SHELL_TOOLS


def shell_command(args):
    if not isinstance(args, dict):
        return ""
    for key in _COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, list):
            joined = " ".join(str(v) for v in value).strip()
            if joined:
                return joined
        elif isinstance(value, str) and value.strip():
            return value.strip()
    return ""
