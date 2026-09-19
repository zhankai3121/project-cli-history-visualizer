"""掃 ~/.claude -> index.db。

兩條管線：
  A. history.jsonl  -> prompt（骨幹，涵蓋所有 session，含過程已被清掉的）
  B. projects/*/*.jsonl -> turn / file_touch / command_run / commit_ref / progress_signal
     （只有還沒被 30 天清理刪掉的 session 有）

用法：
    python indexer.py           # 增量
    python indexer.py --full    # 砍掉重建
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

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
            ("is_container", "INTEGER NOT NULL DEFAULT 0"),
            ("is_scanned", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "progress_signal": [("state", "TEXT")],
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
    shutil.copy2(HISTORY, BACKUP_DIR / f"history-{stamp}.jsonl")

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
        con.execute("UPDATE project SET is_container = ? WHERE id = ?",
                    (int(is_container(row["real_path"], roots)), row["id"]))
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

def _git(path, *args, timeout=10):
    try:
        done = subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def collect_git(con):
    targets = con.execute(
        "SELECT id, real_path FROM project WHERE exists_on_disk = 1 AND is_git = 1"
    ).fetchall()
    updated = 0
    for row in targets:
        path = row["real_path"]
        head = _git(path, "log", "-1", "--format=%ct%x1f%s")
        if head is None:
            continue
        epoch, _, message = head.partition("\x1f")
        last_ts = iso_utc(int(epoch) * 1000) if epoch.isdigit() else None
        porcelain = _git(path, "status", "--porcelain") or ""
        total = _git(path, "rev-list", "--count", "HEAD")
        con.execute(
            "UPDATE project SET git_branch = ?, git_last_ts = ?, git_last_msg = ?, "
            "git_dirty = ?, git_commits = ? WHERE id = ?",
            (_git(path, "rev-parse", "--abbrev-ref", "HEAD"), last_ts, message[:200],
             len([ln for ln in porcelain.splitlines() if ln.strip()]),
             int(total) if total and total.isdigit() else None, row["id"]))
        updated += 1
    return updated


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


def run(full=False):
    if full and DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            Path(str(DB_PATH) + suffix).unlink(missing_ok=True)

    started = time.time()
    con = connect()
    resolver = Resolver(con)

    prompts = index_history(con, resolver)
    con.commit()

    turns = 0
    transcripts = sorted(PROJECTS.glob("*/*.jsonl")) if PROJECTS.exists() else []
    for path in transcripts:
        turns += index_transcript(con, resolver, path)
    con.commit()

    memories = index_memory_files(con, resolver)
    scanned, orphans = scan_project_dirs(con, resolver)
    appeared, vanished = sync_disk_projects(con)
    repos = collect_git(con)
    rollup(con)
    con.commit()

    stats = {k: con.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0]
             for k in ("project", "session", "prompt", "turn", "file_touch",
                       "commit_ref", "progress_signal")}
    stats["live_projects"] = con.execute(
        "SELECT COUNT(*) FROM project WHERE exists_on_disk = 1").fetchone()[0]
    con.close()

    print(f"索引完成 {time.time() - started:.2f}s  "
          f"(+{prompts} prompts, +{turns} turns, +{memories} memory 檔, "
          f"專案 +{appeared} / -{vanished}, 掃描 +{scanned} / 清孤兒 {orphans}, "
          f"git repo {repos})")
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="砍掉重建")
    run(full=ap.parse_args().full)
