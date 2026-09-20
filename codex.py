"""OpenAI Codex CLI 的 session 解析。

格式來源（2026-09 驗證）：
  - 欄位定義：openai/codex `codex-rs/message-history/src/lib.rs`（HistoryEntry）
    與 `codex-rs/rollout/src/rollout_file_name.rs`（檔名規則），Apache-2.0
  - 實際資料：一份公開的 rollout fixture

檔案佈局：
  ~/.codex/history.jsonl                                  {session_id, ts, text}
  ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<id>.jsonl

rollout 每行都是 {timestamp, type, payload}。

⚠ 最重要的一條：真人打的字在 `event_msg` / `user_message`，
   **不是** `response_item` 裡 role=="user" 的那些。後者是系統注入的
   AGENTS.md 全文、<environment_context>、<user_instructions>。
   拿 role 判斷會把整份專案指示當成使用者 prompt 灌進資料庫 ——
   跟 Claude 那邊 <system-reminder> 是同一類陷阱。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")

# apply_patch 的檔案標頭
_PATCH_FILE = re.compile(r"^\*\*\*\s+(Add|Update|Delete)\s+File:\s*(.+?)\s*$", re.M)
_PATCH_VERB = {"Add": "Write", "Update": "Edit", "Delete": "Delete"}
# function_call_output 的結尾狀態
_EXIT_CODE = re.compile(r"Process exited with code (\d+)")
# rollout-2026-02-03T08-02-31-<uuid>.jsonl -> uuid（檔名可能還帶 _suffix）
_ROLLOUT_ID = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def available():
    return (CODEX_HOME / "sessions").is_dir() or (CODEX_HOME / "history.jsonl").is_file()


def history_path():
    path = CODEX_HOME / "history.jsonl"
    return path if path.is_file() else None


def session_files():
    root = CODEX_HOME / "sessions"
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("rollout-*.jsonl") if p.is_file())


def session_id_of(path):
    """從檔名取 thread id；抓不到就退回檔名本身。"""
    hit = _ROLLOUT_ID.search(path.stem)
    return hit.group(1) if hit else path.stem


def iso(value):
    """ts 可能是 epoch 秒 / 毫秒，或已經是 ISO 字串。統一成 ISO UTC。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num > 1e11:
        num /= 1000.0
    return (dt.datetime.fromtimestamp(num, dt.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


# ── history.jsonl ─────────────────────────────────────────────────────────

def history_rows(records):
    """HistoryEntry -> {session_id, ts, text}。

    注意這裡沒有 cwd —— Codex 的 history.jsonl 不記專案路徑（Claude 的有）。
    專案歸屬只能靠 rollout 的 session_meta.cwd 用 session_id 對回來。
    """
    for rec in records:
        if not isinstance(rec, dict):
            continue
        text = rec.get("text")
        session = rec.get("session_id") or rec.get("sessionId")
        ts = iso(rec.get("ts") if rec.get("ts") is not None else rec.get("timestamp"))
        if not text or not session or not ts:
            continue
        yield {"session_id": str(session), "ts": ts, "text": str(text)}


# ── rollout ───────────────────────────────────────────────────────────────

def _text_of(content):
    """content 是 [{type:input_text|output_text, text:…}] 或純字串。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(p for p in parts if p).strip()


def event(rec):
    """一行 rollout -> 正規化事件；認不得就回 None。

    回傳的 kind 與 Claude 那條管線對齊，讓 indexer 兩邊共用寫入邏輯。
    """
    if not isinstance(rec, dict):
        return None
    ts = iso(rec.get("timestamp"))
    outer = rec.get("type")
    body = rec.get("payload")
    if not isinstance(body, dict):
        return None
    inner = body.get("type")

    if outer == "session_meta":
        git = body.get("git") or {}
        return {"kind": "meta", "ts": ts or iso(body.get("timestamp")),
                "session_id": body.get("id"), "cwd": body.get("cwd"),
                "originator": body.get("originator"),
                "cli_version": body.get("cli_version"),
                "git_branch": git.get("branch"), "git_commit": git.get("commit")}

    if outer == "turn_context":
        return {"kind": "meta", "ts": ts, "cwd": body.get("cwd"),
                "model": body.get("model")}

    if outer == "event_msg":
        # 這裡才是真人與 AI 的對話
        if inner == "user_message":
            text = body.get("message")
            return {"kind": "user", "ts": ts, "text": text} if text else None
        if inner == "agent_message":
            text = body.get("message")
            return {"kind": "assistant", "ts": ts, "text": text} if text else None
        if inner == "token_count":
            usage = ((body.get("info") or {}).get("total_token_usage")) or {}
            return {"kind": "tokens", "ts": ts,
                    "input": usage.get("input_tokens"),
                    "output": usage.get("output_tokens")}
        return None

    if outer == "response_item":
        if inner == "function_call":
            return {"kind": "tool", "ts": ts, "name": body.get("name") or "exec",
                    "args": _args(body.get("arguments")),
                    "call_id": body.get("call_id")}
        if inner == "custom_tool_call":
            # apply_patch 走這條，參數在 input 而不是 arguments
            return {"kind": "tool", "ts": ts, "name": body.get("name") or "custom",
                    "args": {"input": body.get("input")},
                    "call_id": body.get("call_id")}
        if inner == "function_call_output":
            out = body.get("output")
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
            code = _EXIT_CODE.search(text or "")
            return {"kind": "tool_output", "ts": ts, "text": text or "",
                    "has_error": bool(code and code.group(1) != "0")}
        if inner == "reasoning":
            parts = [s.get("text") for s in (body.get("summary") or [])
                     if isinstance(s, dict) and s.get("text")]
            return {"kind": "reasoning", "ts": ts, "text": "\n".join(parts)} if parts \
                else None
        # response_item/message 一律不當成對話 —— 那些是注入的指示與環境資訊
        return None

    return None


def _args(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            got = json.loads(raw)
            return got if isinstance(got, dict) else {"command": got}
        except ValueError:
            return {"command": raw}
    return {}


def shell_command(args):
    """function_call 的參數 -> 可讀的指令字串。"""
    for key in ("cmd", "command", "script"):
        value = args.get(key)
        if isinstance(value, list):
            return " ".join(str(v) for v in value)
        if value:
            return str(value)
    return ""


def patch_files(args):
    """apply_patch 的 input -> [(檔案路徑, 動作)]。"""
    blob = args.get("input") or args.get("patch") or ""
    if not isinstance(blob, str):
        return []
    return [(m.group(2), _PATCH_VERB.get(m.group(1), "Edit"))
            for m in _PATCH_FILE.finditer(blob)]


def is_file_tool(name):
    return name in ("apply_patch", "edit_file", "write_file", "str_replace_editor")


def is_shell_tool(name):
    return name in ("exec_command", "shell", "bash", "run_command", "local_shell")
