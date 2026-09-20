"""掃 ~/.claude -> index.db。

兩條管線：
  A. history.jsonl  -> prompt（骨幹，涵蓋所有 session，含過程已被清掉的）
  B. projects/*/*.jsonl -> turn / file_touch / command_run / commit_ref / progress_signal
     （只有還沒被 30 天清理刪掉的 session 有）

用法：
    python indexer.py           # 增量
    python indexer.py --full    # 砍掉重建
    python indexer.py --watch   # 常駐背景，每 5 分鐘跑一次增量
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import codex
import gemini
import parser as P

CLAUDE_DIR = Path.home() / ".claude"
HISTORY = CLAUDE_DIR / "history.jsonl"
PROJECTS = CLAUDE_DIR / "projects"
PASTE_CACHE = CLAUDE_DIR / "paste-cache"

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "index.db"
SCHEMA = ROOT / "schema.sql"
BACKUP_DIR = ROOT / "backup"

MEMORY_FILES = ("brain.md", "MEMORY.md")

# 專案樹的根（可多個，存在 app_config，網頁可以改）。
# 根目錄本身與它的祖先都是「容器」而不是專案 —— 在那些目錄下直接跑 CLI 會產生
# 看起來像專案的雜訊卡片，UI 預設隱藏。
# 首次啟動的預設值：CLIHV_PROJECT_ROOT 環境變數，否則 ~/Desktop/Claude/Project。
DEFAULT_ROOT = Path(os.environ.get("CLIHV_PROJECT_ROOT")
                    or Path.home() / "Desktop" / "Claude" / "Project")

# 掃描專案資料夾時要跳過的目錄名
SKIP_DIRS = {"node_modules", ".venv", "venv", "env", "__pycache__", "vendor",
             "dist", "build", "target", "out", "bin", "obj", "runtime",
             "site-packages", "backup", ".git", ".idea", ".vscode",
             # Windows 使用者設定檔的系統資料夾與相容性 junction，不是專案
             "appdata", "application data", "cookies", "nethood", "printhood",
             "recent", "sendto", "templates", "local settings", "my documents",
             "favorites", "links", "saved games", "searches", "contacts",
             "onedrive", "start menu", "微軟", "3d objects"}


# ── 小工具 ────────────────────────────────────────────────────────────────

def iso_utc(epoch_ms):
    return (dt.datetime.fromtimestamp(epoch_ms / 1000, dt.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def display_name(real_path):
    """<專案根>\\myapp\\完整規劃 -> myapp\\完整規劃"""
    parts = Path(real_path).parts
    for anchor in ("Project", "project"):
        if anchor in parts:
            tail = parts[parts.index(anchor) + 1:]
            if tail:
                return "\\".join(tail)
    return parts[-1] if parts else real_path


def utcnow_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    # 背景 watcher 與 server 會同時寫：WAL 之外再給 5 秒等鎖，別直接丟 "database is locked"
    con.execute("PRAGMA busy_timeout = 5000")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    migrate(con)
    return con


def migrate(con):
    """schema.sql 的 CREATE TABLE IF NOT EXISTS 不會補欄位，這裡補。"""
    wanted = {
        "project": [
            ("exists_on_disk", "INTEGER NOT NULL DEFAULT 0"),
            ("has_history", "INTEGER NOT NULL DEFAULT 0"),
            ("is_git", "INTEGER NOT NULL DEFAULT 0"),
            ("disk_mtime", "TEXT"),
            ("vanished_at", "TEXT"),
            ("git_branch", "TEXT"), ("git_last_ts", "TEXT"),
            ("git_last_msg", "TEXT"), ("git_dirty", "INTEGER"),
            ("git_commits", "INTEGER"),
            ("git_signature", "TEXT"), ("git_checked_at", "TEXT"),
            ("is_container", "INTEGER NOT NULL DEFAULT 0"),
            ("is_scanned", "INTEGER NOT NULL DEFAULT 0"),
            ("is_system", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "file_touch": [("via_agent", "TEXT")],
        "command_run": [("via_agent", "TEXT")],
        "progress_signal": [("state", "TEXT")],
        "session": [("tool", "TEXT NOT NULL DEFAULT 'claude'")],
        "prompt": [("tool", "TEXT NOT NULL DEFAULT 'claude'")],
        "subagent": [("cache_create_tokens", "INTEGER"),
                     ("cache_read_tokens", "INTEGER")],
    }
    for table, columns in wanted.items():
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# ── 專案 / session 解析 ───────────────────────────────────────────────────

class Resolver:
    """real_path -> project_id 快取。絕不從 slug 反推路徑（slug 不可逆）。"""

    def __init__(self, con):
        self.con = con
        self.cache = {}

    def project(self, real_path):
        if not real_path:
            return None
        key = str(real_path).rstrip("\\/")
        if key in self.cache:
            return self.cache[key]
        cur = self.con.execute("SELECT id FROM project WHERE real_path = ?", (key,))
        row = cur.fetchone()
        if row is None:
            cur = self.con.execute(
                "INSERT INTO project(real_path, display_name) VALUES (?, ?)",
                (key, display_name(key)),
            )
            pid = cur.lastrowid
        else:
            pid = row["id"]
        self.cache[key] = pid
        return pid

    def ensure_session(self, session_id, project_id, **fields):
        self.con.execute(
            "INSERT OR IGNORE INTO session(id, project_id) VALUES (?, ?)",
            (session_id, project_id),
        )
        if project_id is not None:
            self.con.execute(
                "UPDATE session SET project_id = COALESCE(project_id, ?) WHERE id = ?",
                (project_id, session_id),
            )
        for column, value in fields.items():
            if value is not None:
                self.con.execute(
                    f"UPDATE session SET {column} = ? WHERE id = ?", (value, session_id)
                )


# ── 增量游標 ──────────────────────────────────────────────────────────────

def scan_cursor(con, path):
    stat = path.stat()
    row = con.execute("SELECT mtime, size, last_offset FROM scan_state WHERE path = ?",
                      (str(path),)).fetchone()
    if row and row["size"] == stat.st_size and row["mtime"] == stat.st_mtime:
        return None                      # 完全沒變，跳過
    if row and stat.st_size >= row["size"]:
        return row["last_offset"]        # append-only，從上次位置續讀
    return 0                             # 檔案縮水（被重寫）-> 重頭來


def save_cursor(con, path, offset):
    stat = path.stat()
    con.execute(
        "INSERT INTO scan_state(path, mtime, size, last_offset) VALUES (?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, "
        "last_offset=excluded.last_offset",
        (str(path), stat.st_mtime, stat.st_size, offset),
    )


# ── 管線 A：history.jsonl ─────────────────────────────────────────────────

def flatten_pasted(value):
    """pastedContents 形狀跨版本不一致，盡力抽出可搜尋的文字。"""
    if not value:
        return None
    chunks = []
    items = value.values() if isinstance(value, dict) else value
    for item in items if isinstance(items, (list, tuple, type({}.values()))) else []:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict):
            for key in ("content", "text", "value"):
                if isinstance(item.get(key), str):
                    chunks.append(item[key])
                    break
            else:
                cached = item.get("id") or item.get("hash")
                if cached:
                    hit = PASTE_CACHE / f"{cached}.txt"
                    if hit.exists():
                        chunks.append(hit.read_text(encoding="utf-8", errors="replace"))
    joined = "\n".join(c for c in chunks if c).strip()
    return joined or None


def index_history(con, resolver):
    if not HISTORY.exists():
        print(f"! 找不到 {HISTORY}", file=sys.stderr)
        return 0

    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = dt.date.today().isoformat()
    backup = BACKUP_DIR / f"history-{stamp}.jsonl"
    # --watch 每輪都會走到這裡：同一天、大小沒變就不用再抄一次
    if not backup.exists() or backup.stat().st_size != HISTORY.stat().st_size:
        shutil.copy2(HISTORY, backup)

    offset = scan_cursor(con, HISTORY)
    if offset is None:
        return 0

    seen, added = {}, 0
    for new_offset, rec in P.iter_jsonl(HISTORY, offset):
        text = rec.get("display")
        session_id = rec.get("sessionId")
        epoch = rec.get("timestamp")
        if not text or not session_id or epoch is None:
            continue
        ts = iso_utc(epoch)
        pid = resolver.project(rec.get("project"))
        resolver.ensure_session(session_id, pid)
        seen[session_id] = seen.get(session_id, 0) + 1
        cur = con.execute(
            "INSERT OR IGNORE INTO prompt"
            "(session_id, project_id, ts, seq, text, is_slash, source, pasted) "
            "VALUES (?,?,?,?,?,?,'history',?)",
            (session_id, pid, ts, seen[session_id], text,
             1 if text.lstrip().startswith("/") else 0,
             flatten_pasted(rec.get("pastedContents"))),
        )
        added += cur.rowcount
        offset = new_offset

    save_cursor(con, HISTORY, offset)
    return added


# ── 管線 B：session transcript ────────────────────────────────────────────

def index_transcript(con, resolver, path):
    session_id = path.stem
    offset = scan_cursor(con, path)
    if offset is None:
        return 0

    project_id = None
    pending_error = False
    turns = 0

    for new_offset, rec in P.iter_jsonl(path, offset):
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        cwd = rec.get("cwd")
        if cwd and project_id is None:
            project_id = resolver.project(cwd)
            resolver.ensure_session(session_id, project_id,
                                    transcript_state="live", transcript_path=str(path))

        if rtype == "assistant" and not rec.get("isSidechain"):
            summary = P.assistant_text(rec)
            tools = [name for name, _ in P.tool_uses(rec) if name]
            if summary or tools:
                con.execute(
                    "INSERT INTO turn(session_id, project_id, ts, assistant_summary, "
                    "tools_json, has_error) VALUES (?,?,?,?,?,?)",
                    (session_id, project_id, ts, summary[:4000] or None,
                     json.dumps(tools, ensure_ascii=False), int(pending_error)),
                )
                turns += 1
                pending_error = False
            _record_usage(con, session_id, project_id, ts, rec)
            for name, params in P.tool_uses(rec):
                _record_tool(con, session_id, project_id, ts, name, params)

        elif rtype == "user":
            if P.has_tool_error(rec):
                pending_error = True

        elif rtype == "system":
            subtype = rec.get("subtype")
            if subtype == "away_summary":
                goal, state, nxt = P.parse_away_summary(rec.get("content"))
                _signal(con, session_id, project_id, ts, "away_summary",
                        goal, nxt, rec.get("content"), "away_summary", state)

        elif rtype == "ai-title":
            title = rec.get("aiTitle")
            if title:
                resolver.ensure_session(session_id, project_id, title=title)

        elif rtype == "last-prompt":
            last = rec.get("lastPrompt")
            if last:
                _signal(con, session_id, project_id, ts, "last_prompt",
                        None, None, last, "last-prompt")

        elif rtype == "cost-state":
            payload = {k: rec.get(k) for k in
                       ("totalLinesAdded", "totalLinesRemoved", "totalDuration",
                        "totalCostUSD")}
            _signal(con, session_id, project_id, ts, "cost_state",
                    None, None, json.dumps(payload, ensure_ascii=False), "cost-state")

        if rtype == "user" and rec.get("isCompactSummary"):
            content = rec.get("message", {}).get("content")
            if isinstance(content, str):
                goal, nxt = P.parse_compact_summary(content)
                _signal(con, session_id, project_id, ts, "compact_summary",
                        goal, nxt, content[:8000], "compact")

        offset = new_offset

    resolver.ensure_session(session_id, project_id,
                            transcript_state="live", transcript_path=str(path))
    save_cursor(con, path, offset)
    return turns


def _record_tool(con, session_id, project_id, ts, name, params):
    if name in P.EDIT_TOOLS:
        target = params.get("file_path") or params.get("notebook_path")
        if target:
            con.execute(
                "INSERT INTO file_touch(session_id, project_id, ts, path, verb) "
                "VALUES (?,?,?,?,?)", (session_id, project_id, ts, target, name))
    elif name in ("Bash", "PowerShell"):
        command = params.get("command")
        if command:
            con.execute(
                "INSERT INTO command_run(session_id, project_id, ts, command, kind) "
                "VALUES (?,?,?,?,?)",
                (session_id, project_id, ts, command[:2000],
                 P.classify_command(command)))
            for message in P.extract_commit_messages(command):
                con.execute(
                    "INSERT INTO commit_ref(session_id, project_id, ts, message) "
                    "VALUES (?,?,?,?)", (session_id, project_id, ts, message[:500]))


# 一次 API 呼叫會被拆成好幾行 assistant（text 一行、tool_use 一行），usage 完全
# 相同 —— 本機 3867 行只對應 1621 個 requestId，不去重會高估 2.4 倍。
# model="<synthetic>" 是 CLI 自己合成的錯誤訊息，沒有真的呼叫過 API。
SYNTHETIC_MODEL = "<synthetic>"


def _record_usage(con, session_id, project_id, ts, rec):
    message = rec.get("message") or {}
    usage = message.get("usage")
    if not isinstance(usage, dict) or message.get("model") == SYNTHETIC_MODEL:
        return
    request_id = rec.get("requestId") or rec.get("uuid")
    if not request_id:
        return
    details = usage.get("output_tokens_details")
    thinking = details.get("thinking_tokens") if isinstance(details, dict) else None
    con.execute(
        "INSERT OR IGNORE INTO api_call(session_id, project_id, ts, request_id, model, "
        "input_tokens, cache_create_tokens, cache_read_tokens, output_tokens, "
        "thinking_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (session_id, project_id, ts, request_id, message.get("model"),
         usage.get("input_tokens") or 0,
         usage.get("cache_creation_input_tokens") or 0,
         usage.get("cache_read_input_tokens") or 0,
         usage.get("output_tokens") or 0,
         thinking or 0),
    )


def backfill_usage(con):
    """舊 DB 的 transcript 早就讀到檔尾，api_call 卻是空的。

    清 scan_state 會讓 turn / file_touch 重複插入（它們沒有 UNIQUE），所以改成
    一次性重讀每個 transcript 的 assistant 行，只寫 api_call（有 UNIQUE 擋重複）。
    """
    if con.execute("SELECT 1 FROM app_config WHERE key = 'usage_backfilled'").fetchone():
        return 0
    sessions = con.execute(
        "SELECT id, project_id, transcript_path FROM session "
        "WHERE tool = 'claude' AND transcript_path IS NOT NULL").fetchall()
    added = 0
    for row in sessions:
        path = Path(row["transcript_path"])
        if not path.is_file():
            continue
        for _, rec in P.iter_jsonl(path, 0):
            if rec.get("type") != "assistant" or rec.get("isSidechain"):
                continue
            before = con.total_changes
            _record_usage(con, row["id"], row["project_id"], rec.get("timestamp"), rec)
            added += con.total_changes - before
    con.execute(
        "INSERT OR REPLACE INTO app_config(key, value) VALUES ('usage_backfilled', '1')")
    return added


def _signal(con, session_id, project_id, ts, kind, goal, nxt, body, origin, state=None):
    con.execute(
        "INSERT OR IGNORE INTO progress_signal"
        "(session_id, project_id, ts, kind, goal, state, next_step, body, origin) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, project_id, ts, kind, goal, state, nxt, body, origin),
    )


# ── 管線 C：使用者手寫的進度檔（對照欄）──────────────────────────────────

def index_memory_files(con, resolver):
    count = 0
    for slug_dir in PROJECTS.iterdir():
        if not slug_dir.is_dir():
            continue
        memory_dir = slug_dir / "memory"
        if not memory_dir.is_dir():
            continue
        # slug 不可逆 -> 靠這個 slug 底下任一 session 的 project_id 回推
        row = con.execute(
            "SELECT project_id FROM session WHERE transcript_path LIKE ? "
            "AND project_id IS NOT NULL LIMIT 1", (f"{slug_dir}%",)).fetchone()
        project_id = row["project_id"] if row else None
        for name in MEMORY_FILES:
            path = memory_dir / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            goal, nxt = P.parse_memory_markdown(text)
            if not (goal or nxt):
                continue
            ts = dt.datetime.fromtimestamp(
                path.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds")
            _signal(con, None, project_id, ts, "memory_file", goal, nxt,
                    text[:4000], name)
            count += 1
    return count


# ── 管線 G：子代理 ────────────────────────────────────────────────────────
#
# 216 MB 的 subagent transcript 裡，值得留的只有三樣：
#   1. meta.json 的交辦內容（做什麼、哪種 agent、什麼模型）
#   2. 最後一則 assistant 發言 —— 那就是回報給主線的結果
#   3. 改過的檔案與跑過的指令 —— 這些是真實的產出，之前完全沒被索引，
#      專案卡上的「改 N 檔」一直在少算
# 中間過程（每一步的搜尋、讀檔、思考）不存，那才是那 200 MB 的來源。

def subagent_files(session_dir):
    return sorted(p for p in session_dir.rglob("agent-*.jsonl")
                  if p.is_file() and "subagents" in p.parts)


def _read_meta(path):
    meta = path.with_name(path.stem + ".meta.json")
    if not meta.is_file():
        return {}
    try:
        got = json.loads(meta.read_text(encoding="utf-8", errors="replace"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def index_subagent(con, resolver, path, session_id, project_id):
    """回傳 (是否有處理, 抽到的檔案編輯數)。"""
    agent_id = path.stem
    if scan_cursor(con, path) is None:
        return False, 0

    meta = _read_meta(path)
    first_ts = last_ts = last_text = None
    turns = tools = 0
    files, commands = [], []
    tok_in = tok_out = tok_cc = tok_cr = 0
    seen_requests = set()
    offset = 0

    for new_offset, rec in P.iter_jsonl(path, 0):
        offset = new_offset
        ts = rec.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if rec.get("type") != "assistant":
            continue
        turns += 1
        text = P.assistant_text(rec)
        if text:
            last_text = text
        usage = rec.get("message", {}).get("usage") or {}
        request_id = rec.get("requestId") or rec.get("uuid")
        if usage and request_id not in seen_requests:   # 同一呼叫會拆成數行
            seen_requests.add(request_id)
            tok_in += usage.get("input_tokens") or 0
            tok_out += usage.get("output_tokens") or 0
            tok_cc += usage.get("cache_creation_input_tokens") or 0
            tok_cr += usage.get("cache_read_input_tokens") or 0
        for name, params in P.tool_uses(rec):
            tools += 1
            if name in P.EDIT_TOOLS:
                target = params.get("file_path") or params.get("notebook_path")
                if target:
                    files.append((ts, target, name))
            elif name in ("Bash", "PowerShell"):
                command = params.get("command")
                if command:
                    commands.append((ts, command))

    con.execute("""
        INSERT INTO subagent(agent_id, session_id, project_id, parent_agent_id,
            agent_type, description, model, tool_use_id, spawn_depth, request_shape,
            workflow_phase, started_at, ended_at, turn_count, tool_count, file_count,
            input_tokens, output_tokens, cache_create_tokens, cache_read_tokens,
            result, path)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(agent_id) DO UPDATE SET
            ended_at=excluded.ended_at, turn_count=excluded.turn_count,
            tool_count=excluded.tool_count, file_count=excluded.file_count,
            input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
            cache_create_tokens=excluded.cache_create_tokens,
            cache_read_tokens=excluded.cache_read_tokens,
            result=excluded.result
    """, (agent_id, session_id, project_id, meta.get("parentAgentId"),
          meta.get("agentType"), meta.get("description"), meta.get("model"),
          meta.get("toolUseId"), meta.get("spawnDepth"), meta.get("requestShape"),
          meta.get("workflowPhase"), first_ts, last_ts, turns, tools,
          len({f[1] for f in files}), tok_in or None, tok_out or None,
          tok_cc or None, tok_cr or None,
          (last_text or "")[:8000] or None, str(path)))

    # 子代理的產出併進主線的統計，但標記來源，需要時分得開
    con.execute("DELETE FROM file_touch WHERE via_agent = ?", (agent_id,))
    con.execute("DELETE FROM command_run WHERE via_agent = ?", (agent_id,))
    for ts, target, verb in files:
        con.execute(
            "INSERT INTO file_touch(session_id, project_id, ts, path, verb, via_agent) "
            "VALUES (?,?,?,?,?,?)", (session_id, project_id, ts, target, verb, agent_id))
    for ts, command in commands:
        con.execute(
            "INSERT INTO command_run(session_id, project_id, ts, command, kind, via_agent) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, project_id, ts, command[:2000],
             P.classify_command(command), agent_id))
        for message in P.extract_commit_messages(command):
            con.execute(
                "INSERT INTO commit_ref(session_id, project_id, ts, message) "
                "VALUES (?,?,?,?)", (session_id, project_id, ts, message[:500]))

    save_cursor(con, path, offset)
    return True, len(files)


def index_subagents(con, resolver):
    """掃每個 session 目錄底下的 subagents/。回傳 (代理數, 檔案編輯數)。"""
    if not PROJECTS.exists():
        return 0, 0
    agents = edits = 0
    for session_dir in PROJECTS.glob("*/*/"):
        if not (session_dir / "subagents").is_dir():
            continue
        session_id = session_dir.name
        row = con.execute("SELECT project_id FROM session WHERE id = ?",
                          (session_id,)).fetchone()
        project_id = row["project_id"] if row else None
        for path in subagent_files(session_dir):
            done, n = index_subagent(con, resolver, path, session_id, project_id)
            agents += done
            edits += n
    return agents, edits


# ── 管線 E：Codex CLI ─────────────────────────────────────────────────────
#
# 與 Claude 的差別：
#   - Codex 的 history.jsonl 只有 {session_id, ts, text}，沒有 cwd。專案歸屬
#     只能靠 rollout 的 session_meta.cwd 對回來，所以 rollout 要先跑。
#   - 真人 prompt 在 event_msg/user_message，不是 response_item 的 role=="user"
#     （那些是注入的 AGENTS.md 與環境資訊）。判別式在 codex.py。
#   - 沒有 away_summary / ai-title 這種現成摘要，所以「狀態」只能取最後一則
#     agent_message，goal / next_step 留空。UI 已經能處理缺訊號的情況。

def index_codex_rollout(con, resolver, path):
    session_id = codex.session_id_of(path)
    offset = scan_cursor(con, path)
    if offset is None:
        return 0

    project_id = None
    pending_error = False
    turns = 0
    last_ts = None

    for new_offset, rec in P.iter_jsonl(path, offset):
        ev = codex.event(rec)
        offset = new_offset
        if not ev:
            continue
        ts = ev.get("ts") or last_ts
        last_ts = ts or last_ts

        if ev["kind"] == "meta":
            if ev.get("cwd") and project_id is None:
                project_id = resolver.project(ev["cwd"])
                resolver.ensure_session(session_id, project_id, tool="codex",
                                        transcript_state="live",
                                        transcript_path=str(path))
            continue

        if ev["kind"] == "user":
            con.execute(
                "INSERT OR IGNORE INTO prompt"
                "(session_id, project_id, ts, seq, text, is_slash, source, tool) "
                "VALUES (?,?,?,0,?,?, 'transcript', 'codex')",
                (session_id, project_id, ts, ev["text"],
                 1 if ev["text"].lstrip().startswith("/") else 0))
            continue

        if ev["kind"] == "assistant":
            con.execute(
                "INSERT INTO turn(session_id, project_id, ts, assistant_summary, "
                "tools_json, has_error) VALUES (?,?,?,?,?,?)",
                (session_id, project_id, ts, ev["text"][:4000], "[]",
                 int(pending_error)))
            turns += 1
            pending_error = False
            continue

        if ev["kind"] == "tool":
            name, args = ev["name"], ev["args"]
            if codex.is_file_tool(name):
                for target, verb in codex.patch_files(args):
                    con.execute(
                        "INSERT INTO file_touch(session_id, project_id, ts, path, verb) "
                        "VALUES (?,?,?,?,?)", (session_id, project_id, ts, target, verb))
            elif codex.is_shell_tool(name):
                command = codex.shell_command(args)
                if command:
                    con.execute(
                        "INSERT INTO command_run(session_id, project_id, ts, command, kind) "
                        "VALUES (?,?,?,?,?)",
                        (session_id, project_id, ts, command[:2000],
                         P.classify_command(command)))
                    for message in P.extract_commit_messages(command):
                        con.execute(
                            "INSERT INTO commit_ref(session_id, project_id, ts, message) "
                            "VALUES (?,?,?,?)",
                            (session_id, project_id, ts, message[:500]))
            continue

        if ev["kind"] == "tool_output" and ev.get("has_error"):
            pending_error = True

    resolver.ensure_session(session_id, project_id, tool="codex",
                            transcript_state="live", transcript_path=str(path))
    save_cursor(con, path, offset)
    return turns


def index_codex_history(con, resolver):
    """history.jsonl 補上 rollout 已經被刪掉的那些 session 的 prompt。

    沒有 cwd，所以 project_id 從 session 表對回來；對不到就留空。
    """
    path = codex.history_path()
    if not path:
        return 0
    offset = scan_cursor(con, path)
    if offset is None:
        return 0

    added, seen = 0, {}
    for new_offset, rec in P.iter_jsonl(path, offset):
        for row in codex.history_rows([rec]):
            sid = row["session_id"]
            resolver.ensure_session(sid, None, tool="codex")
            known = con.execute("SELECT project_id FROM session WHERE id = ?",
                                (sid,)).fetchone()
            pid = known["project_id"] if known else None
            seen[sid] = seen.get(sid, 0) + 1
            cur = con.execute(
                "INSERT OR IGNORE INTO prompt"
                "(session_id, project_id, ts, seq, text, is_slash, source, tool) "
                "VALUES (?,?,?,?,?,?, 'history', 'codex')",
                (sid, pid, row["ts"], seen[sid], row["text"],
                 1 if row["text"].lstrip().startswith("/") else 0))
            added += cur.rowcount
        offset = new_offset

    save_cursor(con, path, offset)
    return added


def index_codex(con, resolver):
    """回傳 (prompt 數, turn 數, rollout 檔數)。沒裝 Codex 就整段跳過。"""
    if not codex.available():
        return 0, 0, 0
    files = codex.session_files()
    turns = sum(index_codex_rollout(con, resolver, p) for p in files)
    prompts = index_codex_history(con, resolver)   # rollout 先跑，才有 cwd 可對
    return prompts, turns, len(files)


# ── 管線 H：Gemini CLI ────────────────────────────────────────────────────
#
# 與另外兩條的差別（實測依據見 docs/gemini-schema.md）：
#   - 檔案雖然是 append-only，但 `$set.messages` 會整批重設訊息表、`$rewindTo`
#     會砍掉尾巴。游標只能拿來判斷「有沒有變」，一變就整檔從頭重播，並先刪掉
#     這個 session 已經寫進去的列再重插（比照 index_subagent 的做法）。
#   - 真人 prompt 要擋掉 CLI 注入的 <session_context>（判別式在 gemini.py）。
#   - 專案路徑不在 session 檔裡，而在同一個 tmp/<slug>/ 底下的 .project_root，
#     而且內容**全小寫** —— 直接拿去建 project 會多出一張大小寫不同的重複卡。


def gemini_project(con, resolver, root_text):
    """`.project_root`（全小寫）-> project_id，不製造大小寫重複的卡片。

    1. 資料夾還在 -> `os.path.realpath()` 取回磁碟上的真實大小寫。
    2. 不在了 -> 用 `COLLATE NOCASE` 對既有的 project 列。
    3. 都沒有 -> 只好照原樣建一列。
    """
    if not root_text:
        return None
    key = str(root_text).rstrip("\\/")
    if os.path.exists(key):
        return resolver.project(os.path.realpath(key))
    row = con.execute("SELECT id FROM project WHERE real_path = ? COLLATE NOCASE",
                      (key,)).fetchone()
    return row["id"] if row else resolver.project(key)


def _record_gemini_tool(con, session_id, project_id, ts, ev):
    name, args = ev["name"], ev["args"]
    if gemini.is_file_tool(name):
        target = gemini.file_target(args)
        if target:
            con.execute(
                "INSERT INTO file_touch(session_id, project_id, ts, path, verb) "
                "VALUES (?,?,?,?,?)",
                (session_id, project_id, ts, target, gemini.file_verb(name)))
    elif gemini.is_shell_tool(name):
        command = gemini.shell_command(args)
        if command:
            con.execute(
                "INSERT INTO command_run(session_id, project_id, ts, command, kind) "
                "VALUES (?,?,?,?,?)",
                (session_id, project_id, ts, command[:2000],
                 P.classify_command(command)))
            for message in P.extract_commit_messages(command):
                con.execute(
                    "INSERT INTO commit_ref(session_id, project_id, ts, message) "
                    "VALUES (?,?,?,?)", (session_id, project_id, ts, message[:500]))


def index_gemini_session(con, resolver, path):
    """一個 chats/*.jsonl -> (新增的 prompt 數, turn 數)。檔案沒變就整份跳過。"""
    if scan_cursor(con, path) is None:
        return 0, 0

    offset, records = 0, []
    for new_offset, rec in P.iter_jsonl(path, 0):     # 永遠從頭讀，理由見上面
        records.append(rec)
        offset = new_offset

    meta, messages = gemini.replay(records)
    session_id = gemini.session_id_of(path, meta.get("sessionId"))

    # 重播是「整份重來」，舊的列一定要先清掉，否則每次檔案變動都多一份。
    # 清在 worth_indexing 之前：檔案被 $set / $rewindTo 洗成空的，舊列也要跟著走。
    for table in ("prompt", "turn", "file_touch", "command_run", "commit_ref"):
        con.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))

    if not gemini.worth_indexing(messages):
        con.execute("DELETE FROM session WHERE id = ?", (session_id,))
        save_cursor(con, path, offset)                # 孤兒檔，記下來別再重讀
        return 0, 0

    project_id = gemini_project(con, resolver, gemini.project_root_of(path))
    resolver.ensure_session(session_id, project_id, tool="gemini",
                            transcript_state="live", transcript_path=str(path))

    prompts = turns = seq = 0
    pending_error = False
    last_ts = meta.get("startTime")

    for msg in messages:
        found = gemini.events(msg)
        names = [e["name"] for e in found if e["kind"] == "tool" and e["name"]]
        summary = ""
        turn_ts = msg.get("timestamp") or last_ts

        for ev in found:
            ts = ev.get("ts") or last_ts
            last_ts = ts or last_ts
            if ev["kind"] == "user":
                seq += 1
                cur = con.execute(
                    "INSERT OR IGNORE INTO prompt"
                    "(session_id, project_id, ts, seq, text, is_slash, source, tool) "
                    "VALUES (?,?,?,?,?,?, 'transcript', 'gemini')",
                    (session_id, project_id, ts, seq, ev["text"],
                     1 if ev["text"].lstrip().startswith("/") else 0))
                prompts += cur.rowcount
            elif ev["kind"] == "tool":
                _record_gemini_tool(con, session_id, project_id, ts, ev)
            elif ev["kind"] == "tool_output":
                pending_error = pending_error or bool(ev.get("has_error"))
            elif ev["kind"] == "assistant":
                summary = ev["text"]

        if summary or names:
            con.execute(
                "INSERT INTO turn(session_id, project_id, ts, assistant_summary, "
                "tools_json, has_error) VALUES (?,?,?,?,?,?)",
                (session_id, project_id, turn_ts, summary[:4000] or None,
                 json.dumps(names, ensure_ascii=False), int(pending_error)))
            turns += 1
            pending_error = False

    save_cursor(con, path, offset)
    return prompts, turns


def index_gemini(con, resolver):
    """回傳 (prompt 數, turn 數, session 檔數)。沒裝 Gemini CLI 就整段跳過。"""
    if not gemini.available():
        return 0, 0, 0
    files = gemini.session_files()
    prompts = turns = 0
    for path in files:
        added, made = index_gemini_session(con, resolver, path)
        prompts += added
        turns += made
    return prompts, turns, len(files)


# ── 管線 D：磁碟狀態對帳（資料夾刪掉就從網頁消失）────────────────────────
#
# 專案來源仍然是「跑過 CLI 的」（history.jsonl + transcript 的 cwd）。
# 這裡不做資料夾探索 —— 沒跑過 CLI 的資料夾不是專案，不該混進來。
# 這一段只回答一個問題：資料庫裡這些專案，資料夾還在不在。

def get_roots(con):
    """目前設定的專案根目錄清單。第一次讀取時寫入預設值。"""
    row = con.execute("SELECT value FROM app_config WHERE key = 'project_roots'").fetchone()
    if row:
        try:
            roots = json.loads(row["value"])
            if isinstance(roots, list):
                return [str(r) for r in roots if r]
        except ValueError:
            pass
    set_roots(con, [str(DEFAULT_ROOT)])
    return [str(DEFAULT_ROOT)]


def set_roots(con, roots):
    clean, seen = [], set()
    for r in roots:
        r = str(r).rstrip("\\/") or str(r)
        if r.lower() not in seen:
            seen.add(r.lower())
            clean.append(r)
    con.execute(
        "INSERT INTO app_config(key, value) VALUES ('project_roots', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(clean, ensure_ascii=False),))
    return clean


# 系統目錄：在這些地方跑過 CLI 會生出看起來像專案的卡片（例如從 system32
# 開的終端機）。不是專案，UI 預設隱藏。
_SYS_WIN = re.compile(
    r"^[a-z]:/(windows|program files( \(x86\))?|programdata|"
    r"\$recycle\.bin|system volume information|perflogs)(/|$)")
_SYS_NIX = re.compile(
    r"^/(usr|etc|bin|sbin|lib|lib64|var|opt|proc|sys|dev|boot|run|srv|snap|"
    r"tmp|lost\+found)(/|$)")
# WSL 的 UNC 前綴：\\wsl.localhost\<發行版> 或 \\wsl$\<發行版>
_WSL_PREFIX = re.compile(r"^//wsl(\.localhost|\$)/[^/]+")


def is_system_path(real_path):
    """這個路徑是不是作業系統自己的目錄。"""
    path = str(real_path).replace("\\", "/").lower().rstrip("/")
    if _SYS_WIN.match(path):
        return True
    # WSL 裡的 /usr、/etc… 也算；把 UNC 前綴剝掉再比
    inside = _WSL_PREFIX.sub("", path)
    return bool(_SYS_NIX.match(inside if inside.startswith("/") else path))


def is_container(real_path, roots):
    """任何一個根目錄本身，或它的祖先目錄 → 容器，不是專案。"""
    here = str(real_path).rstrip("\\/").lower()
    for root in roots:
        r = str(root).rstrip("\\/").lower()
        if here == r or r.startswith(here + "\\") or r.startswith(here + "/"):
            return True
    return False


# ── 管線 F：掃描指定資料夾，把裡面的子資料夾當專案 ────────────────────────
#
# 深度 1 的子資料夾一律算專案；深度 2 只認「自帶 .git / .claude」或「已經有
# CLI 紀錄」的 —— 否則 src/ tests/ 這種會被誤認成專案。

def _is_project_dir(path):
    return (path.is_dir()
            and not path.name.startswith(".")
            and path.name.lower() not in SKIP_DIRS)


def scan_project_dirs(con, resolver):
    """掃描設定的根目錄，把找到的專案資料夾登記進 project 表。

    回傳 (新增, 清掉的孤兒)。移除根目錄之後，當初靠掃描登記、又沒有任何
    CLI 資料的專案會被刪掉 —— 否則設錯一次根目錄（例如整個家目錄）就會
    永久留下一堆 AppData / Downloads 之類的假專案。
    """
    known = {r["real_path"].lower()
             for r in con.execute("SELECT real_path FROM project")}
    found, added = [], 0

    for root in get_roots(con):
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            if not _is_project_dir(child):
                continue
            found.append(child)
            try:
                grandchildren = sorted(child.iterdir())
            except OSError:
                continue
            for grand in grandchildren:
                if not _is_project_dir(grand):
                    continue
                if ((grand / ".git").exists() or (grand / ".claude").exists()
                        or str(grand).lower() in known):
                    found.append(grand)

    live = set()
    for path in found:
        if str(path).lower() not in known:
            added += 1
        pid = resolver.project(str(path))
        con.execute("UPDATE project SET is_scanned = 1 WHERE id = ?", (pid,))
        live.add(pid)

    # 清孤兒：當初靠掃描登記、現在不在任何根目錄底下、又完全沒有 CLI 資料的，
    # 直接刪掉。任何一張表還參照到就保留（外鍵會擋，而且那代表真的有資料），
    # 只取消 is_scanned 標記。
    refs = ("prompt", "session", "turn", "file_touch", "command_run",
            "commit_ref", "progress_signal")
    unreferenced = " AND ".join(
        f"id NOT IN (SELECT project_id FROM {t} WHERE project_id IS NOT NULL)"
        for t in refs)
    orphans = [r["id"] for r in
               con.execute(f"SELECT id FROM project WHERE is_scanned = 1 AND {unreferenced}")
               if r["id"] not in live]
    for pid in orphans:
        con.execute("DELETE FROM project WHERE id = ?", (pid,))
    con.execute(
        "UPDATE project SET is_scanned = 0 WHERE is_scanned = 1 AND id NOT IN (%s)"
        % (",".join("?" * len(live)) or "NULL"), tuple(live))

    resolver.cache.clear()      # 刪過 row，快取的 id 可能失效
    return added, len(orphans)


def sync_disk_projects(con):
    """對帳磁碟與資料庫。回傳 (重新出現, 消失) 兩個數字。

    消失的專案是軟刪除（exists_on_disk=0 + vanished_at），UI 預設不顯示。
    不真的 DELETE —— 那會連帶毀掉該專案所有 prompt 歷史。資料夾放回來就會復原。
    """
    now = utcnow_iso()
    before = {r["real_path"] for r in
              con.execute("SELECT real_path FROM project WHERE exists_on_disk = 1")}

    roots = get_roots(con)
    appeared = 0
    for row in con.execute("SELECT id, real_path FROM project").fetchall():
        path = Path(row["real_path"])
        con.execute("UPDATE project SET is_container = ?, is_system = ? WHERE id = ?",
                    (int(is_container(row["real_path"], roots)),
                     int(is_system_path(row["real_path"])), row["id"]))
        alive = path.is_dir()
        if alive:
            if row["real_path"] not in before:
                appeared += 1
            mtime = dt.datetime.fromtimestamp(
                path.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds")
            con.execute(
                "UPDATE project SET exists_on_disk = 1, vanished_at = NULL, "
                "is_git = ?, disk_mtime = ? WHERE id = ?",
                (1 if (path / ".git").exists() else 0, mtime, row["id"]))
        else:
            con.execute(
                "UPDATE project SET exists_on_disk = 0, "
                "vanished_at = COALESCE(vanished_at, ?) WHERE id = ?", (now, row["id"]))

    after = {r["real_path"] for r in
             con.execute("SELECT real_path FROM project WHERE exists_on_disk = 1")}
    return appeared, len(before - after)


# ── 管線 E：git 聯動 ──────────────────────────────────────────────────────
#
# transcript 裡只抓得到 2 筆 git commit（大多數 commit 不是透過 Bash 工具下的），
# 所以「有沒有真的落地」要直接問 repo 本身，不能只信對話紀錄。

def _git(path, *args, timeout=25):
    """跑一個唯讀 git 指令。失敗一律回 None，絕不拋例外。

    `-c safe.directory=<path>`：WSL / 網路磁碟機上的 repo 擁有者 UID 與
    Windows 帳號不同，git 會擋下來說 "detected dubious ownership"。這裡
    只對使用者自己登記的那個目錄放行，不用 `*`，也不動使用者的 git config。
    UNC 上 `status --porcelain` 實測要 2 秒出頭，所以 timeout 放寬。
    """
    try:
        done = subprocess.run(
            ["git", "-c", f"safe.directory={path}", "-C", str(path), *args],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


GIT_TTL_SECONDS = 600


def git_signature(real_path):
    """.git 底下幾個檔案的 mtime 合起來當指紋。

    commit / checkout / add 都會動到其中之一。純粹改工作區的檔案不會，
    所以還要搭配 TTL —— 這是刻意的取捨：每次都跑 `git status` 太慢
    （WSL 上單一 repo 就要 0.5–0.9 秒）。按 ↻ 或 --full 會強制重讀。
    """
    dot = Path(real_path) / ".git"
    try:
        if dot.is_file():                 # worktree / submodule 的 .git 是檔案
            return str(dot.stat().st_mtime_ns)
    except OSError:
        return None
    parts = []
    for name in ("HEAD", "index", "packed-refs", "refs"):
        try:
            parts.append(str((dot / name).stat().st_mtime_ns))
        except OSError:
            parts.append("-")
    return "|".join(parts) if any(p != "-" for p in parts) else None


def _status_v2(path):
    """一次呼叫同時拿到分支與未提交檔案數（比分開跑省一次 git）。"""
    out = _git(path, "status", "--porcelain=v2", "--branch")
    if out is None:
        return None, None
    branch, dirty = None, 0
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head "):].strip()
        elif line and not line.startswith("#"):
            dirty += 1
    return branch, dirty


def collect_git(con, force=False):
    """回傳 (實際重讀的 repo 數, 靠快取跳過的數量)。"""
    targets = con.execute(
        "SELECT id, real_path, git_signature, git_checked_at "
        "FROM project WHERE exists_on_disk = 1 AND is_git = 1").fetchall()
    now = dt.datetime.now(dt.timezone.utc)
    updated = skipped = 0

    for row in targets:
        path = row["real_path"]
        signature = git_signature(path)

        if not force and signature and signature == row["git_signature"]:
            fresh = False
            if row["git_checked_at"]:
                try:
                    age = (now - dt.datetime.fromisoformat(
                        row["git_checked_at"])).total_seconds()
                    fresh = age < GIT_TTL_SECONDS
                except ValueError:
                    pass
            if fresh:
                skipped += 1
                continue

        head = _git(path, "log", "-1", "--format=%ct%x1f%s")
        if head is None:
            continue
        epoch, _, message = head.partition("\x1f")
        last_ts = iso_utc(int(epoch) * 1000) if epoch.isdigit() else None
        branch, dirty = _status_v2(path)
        total = _git(path, "rev-list", "--count", "HEAD")

        # 指紋要在跑完 git 之後才取 —— `git status` 自己可能重寫 .git/index，
        # 先取的話下次比對必定不同，快取等於失效
        con.execute(
            "UPDATE project SET git_branch = ?, git_last_ts = ?, git_last_msg = ?, "
            "git_dirty = ?, git_commits = ?, git_signature = ?, git_checked_at = ? "
            "WHERE id = ?",
            (branch, last_ts, message[:200], dirty,
             int(total) if total and total.isdigit() else None,
             git_signature(path), now.isoformat(timespec="seconds"), row["id"]))
        updated += 1
    return updated, skipped


# ── 匯總 ──────────────────────────────────────────────────────────────────

def rollup(con):
    con.executescript("""
        UPDATE session SET
            prompt_count = (SELECT COUNT(*) FROM prompt WHERE prompt.session_id = session.id),
            started_at   = (SELECT MIN(ts)  FROM prompt WHERE prompt.session_id = session.id),
            ended_at     = (SELECT MAX(ts)  FROM prompt WHERE prompt.session_id = session.id);

        UPDATE project SET
            session_count = (SELECT COUNT(*) FROM session WHERE session.project_id = project.id),
            prompt_count  = (SELECT COUNT(*) FROM prompt  WHERE prompt.project_id  = project.id),
            first_seen    = (SELECT MIN(ts)  FROM prompt  WHERE prompt.project_id  = project.id),
            last_seen     = (SELECT MAX(ts)  FROM prompt  WHERE prompt.project_id  = project.id);

        UPDATE project SET has_history = (prompt_count > 0);
    """)


def run(full=False, force_git=False):
    # --full 砍掉整個 DB 重建，但使用者設定（專案根目錄）不該跟著陪葬。
    # 先抄出來，建好新 DB 再寫回去。
    saved_config = {}
    if full and DB_PATH.exists():
        try:
            old = sqlite3.connect(DB_PATH)
            old.row_factory = sqlite3.Row
            saved_config = {r["key"]: r["value"]
                            for r in old.execute("SELECT key, value FROM app_config")}
            old.close()
        except sqlite3.Error:
            pass
        for suffix in ("", "-wal", "-shm"):
            Path(str(DB_PATH) + suffix).unlink(missing_ok=True)

    started = time.time()
    con = connect()
    # 兩個 run() 同時跑（背景 --watch + 網頁 ↻）時，各自在 autocommit 下讀完
    # scan_state 游標才寫入，turn / file_touch 沒有 UNIQUE，會插成兩份。
    # 所以每段管線在讀游標之前就用 BEGIN IMMEDIATE 搶寫鎖，後到的等前者
    # commit 才讀，讀到的就是更新後的游標。整段索引可能超過 5 秒，等久一點。
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("BEGIN IMMEDIATE")
    for key, value in saved_config.items():
        con.execute("INSERT OR REPLACE INTO app_config(key, value) VALUES (?, ?)",
                    (key, value))
    resolver = Resolver(con)

    prompts = index_history(con, resolver)
    con.commit()

    con.execute("BEGIN IMMEDIATE")
    turns = 0
    transcripts = sorted(PROJECTS.glob("*/*.jsonl")) if PROJECTS.exists() else []
    for path in transcripts:
        turns += index_transcript(con, resolver, path)
    backfill_usage(con)          # 舊 DB 的 transcript 已讀完，api_call 還是空的
    con.commit()

    con.execute("BEGIN IMMEDIATE")
    agents, agent_edits = index_subagents(con, resolver)
    con.commit()

    con.execute("BEGIN IMMEDIATE")
    cx_prompts, cx_turns, cx_files = index_codex(con, resolver)
    con.commit()

    con.execute("BEGIN IMMEDIATE")
    gm_prompts, gm_turns, gm_files = index_gemini(con, resolver)
    con.commit()

    con.execute("BEGIN IMMEDIATE")
    memories = index_memory_files(con, resolver)
    scanned, orphans = scan_project_dirs(con, resolver)
    appeared, vanished = sync_disk_projects(con)
    repos, cached = collect_git(con, force=full or force_git)
    rollup(con)
    con.commit()

    stats = {k: con.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0]
             for k in ("project", "session", "prompt", "turn", "file_touch",
                       "commit_ref", "progress_signal", "subagent")}
    stats["live_projects"] = con.execute(
        "SELECT COUNT(*) FROM project WHERE exists_on_disk = 1").fetchone()[0]
    con.close()

    codex_note = (f", Codex +{cx_prompts} prompts / +{cx_turns} turns / "
                  f"{cx_files} rollout") if cx_files or cx_prompts else ""
    gemini_note = (f", Gemini +{gm_prompts} prompts / +{gm_turns} turns / "
                   f"{gm_files} session") if gm_files or gm_prompts else ""
    print(f"索引完成 {time.time() - started:.2f}s  "
          f"(+{prompts} prompts, +{turns} turns, +{memories} memory 檔, "
          f"專案 +{appeared} / -{vanished}, 掃描 +{scanned} / 清孤兒 {orphans}, "
          f"git repo {repos}{f'（{cached} 個用快取）' if cached else ''}"
          f"{f', 子代理 {agents} / 其改檔 {agent_edits}' if agents else ''}"
          f"{codex_note}{gemini_note})")
    print("  " + " · ".join(f"{k} {v}" for k, v in stats.items()))
    return stats


def sync_only(con):
    """server 每次要專案清單時呼叫：掃資料夾 + stat，不重解析 jsonl。"""
    scanned, orphans = scan_project_dirs(con, Resolver(con))
    appeared, vanished = sync_disk_projects(con)
    if scanned or orphans or appeared or vanished:
        rollup(con)
    con.commit()
    return appeared, vanished


def watch(interval=300, force_git=False, max_runs=None):
    """背景蒸餾：每 interval 秒跑一次增量索引，Ctrl+C 停止，回傳跑了幾輪。

    transcript 30 天後會被 CLI 自己清掉，索引不能只在有人開網頁時才發生。
    一輪出事（DB 被鎖、檔案讀壞）只印一行就進下一輪，不退出。
    """
    runs, last = 0, None
    try:
        while max_runs is None or runs < max_runs:
            runs += 1
            quiet = io.StringIO()          # run() 每輪印兩行，太吵；有新東西才由這裡轉述
            try:
                with contextlib.redirect_stdout(quiet):
                    stats = run(full=False, force_git=force_git)
            except sqlite3.OperationalError as exc:
                note = ("DB 忙，下一輪再試" if "locked" in str(exc).lower()
                        else f"DB 出錯，下一輪再試：{exc}")
                print(f"[{utcnow_iso()}] {note}", flush=True)
            except Exception as exc:
                print(f"[{utcnow_iso()}] 索引失敗，下一輪再試：{exc}", flush=True)
            else:
                added = ({k: stats[k] - last[k] for k in ("prompt", "turn")}
                         if last else None)
                if added is None:
                    print(f"[{utcnow_iso()}] 開始盯著看："
                          f"{stats['prompt']} prompts / {stats['turn']} turns", flush=True)
                elif added["prompt"] or added["turn"]:
                    print(f"[{utcnow_iso()}] +{added['prompt']} prompts / "
                          f"+{added['turn']} turns", flush=True)
                last = stats
            if max_runs is not None and runs >= max_runs:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"[{utcnow_iso()}] 收到 Ctrl+C，停止", flush=True)
    return runs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="砍掉重建")
    ap.add_argument("--force-git", action="store_true",
                    help="忽略快取，重讀所有 repo 的 git 狀態")
    ap.add_argument("--watch", action="store_true",
                    help="常駐背景，每 --interval 秒跑一次增量")
    ap.add_argument("--interval", type=int, default=300,
                    help="--watch 的間隔秒數（預設 300，最小 30）")
    ap.add_argument("--max-runs", type=int, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.watch and args.full:
        # --full 會刪掉 DB 檔，讓正在跑的 server 抱著失效的 fd
        ap.error("--watch 不能和 --full 一起用；要重建請先關掉 watcher 與 server")
    if args.watch:
        # --max-runs 是測試用的暗門，真的常駐時才強制最小間隔
        interval = max(args.interval, 0) if args.max_runs is not None else max(args.interval, 30)
        watch(interval=interval, force_git=args.force_git, max_runs=args.max_runs)
    else:
        run(full=args.full, force_git=args.force_git)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
