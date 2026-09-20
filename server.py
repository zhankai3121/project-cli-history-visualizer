"""CLI History Visualizer — 本機 server。

    python server.py      ->  http://127.0.0.1:8787

每次要專案清單時會順手 stat 一次資料夾（sync_only），所以資料夾刪掉、
放回來都會即時反映在網頁上。完整重新索引走 POST /api/reindex。
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

import indexer

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web" / "index.html"
PORT = 8787

# 進度來源優先序：CLI 自己寫的摘要 > 壓縮摘要 > 使用者手寫的 memory 檔
KIND_RANK = {"away_summary": 3, "compact_summary": 2, "memory_file": 1}

app = FastAPI(title="CLI History Visualizer")


def db():
    con = sqlite3.connect(indexer.DB_PATH)
    con.row_factory = sqlite3.Row
    # 背景 watcher 可能正在寫：等最多 5 秒，別直接丟 "database is locked"
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def like_pattern(term):
    """給 LIKE ... ESCAPE '\\' 用的樣式，跳脫 % _ 與反斜線本身。"""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def traffic_light(proj):
    """prompt 講的事情到底有沒有落地 —— 問 repo，不問對話紀錄。

    green  最後一次 commit 在最後一則 prompt 之後 -> 已落實
    yellow 工作區有未提交的變更 -> 正在產出中
    grey   談過但 commit 沒跟上
    None   無從判斷：不是 git repo、git 讀不到、或根本沒有 CLI 紀錄可對照

    最後那個 None 很重要：以前 git 讀失敗會掉進 grey，等於把「不知道」
    報成「沒跟上」。WSL 上的 repo 因為 safe.directory 讀不到時就踩到這個。
    """
    if not proj.get("is_git"):
        return None
    if proj.get("git_last_ts") is None:      # git 指令失敗 -> 不知道，不是沒跟上
        return None
    if proj.get("git_dirty"):
        return "yellow"
    last_commit, last_prompt = proj.get("git_last_ts"), proj.get("last_seen")
    if not last_prompt:                      # 沒有 prompt 可對照
        return None
    return "green" if last_commit >= last_prompt else "grey"


def first_para(text, limit=220):
    if not text:
        return None
    head = next((p.strip() for p in text.split("\n\n") if p.strip()), text.strip())
    return head[:limit] + ("…" if len(head) > limit else "")


# ── API ───────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(WEB)


@app.get("/api/overview")
def overview(include_gone: bool = False, include_containers: bool = False,
             only_history: bool = False):
    con = db()
    appeared, vanished = indexer.sync_only(con)

    clauses = []
    if not include_gone:
        clauses.append("p.exists_on_disk = 1")
    if not include_containers:
        clauses.append("p.is_container = 0 AND p.is_system = 0")
    if only_history:
        clauses.append("p.has_history = 1")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    projects = rows(con.execute(f"""
        SELECT p.id, p.real_path, p.display_name, p.last_seen, p.first_seen,
               p.session_count, p.prompt_count, p.exists_on_disk, p.is_git,
               p.vanished_at, p.git_branch, p.git_last_ts, p.git_last_msg,
               p.git_dirty, p.git_commits, p.is_container, p.is_scanned,
               p.is_system, p.has_history,
               (SELECT COUNT(*) FROM commit_ref c WHERE c.project_id = p.id) AS commits,
               (SELECT COUNT(DISTINCT path) FROM file_touch f WHERE f.project_id = p.id) AS files,
               (SELECT COUNT(DISTINCT path) FROM file_touch f
                 WHERE f.project_id = p.id AND f.via_agent IS NOT NULL) AS agent_files,
               (SELECT COUNT(*) FROM subagent a WHERE a.project_id = p.id) AS agents,
               (SELECT COALESCE(SUM(k.output_tokens), 0) FROM api_call k
                 WHERE k.project_id = p.id) AS tokens_out,
               (SELECT COALESCE(SUM(k.input_tokens + k.cache_create_tokens
                                    + k.output_tokens), 0) FROM api_call k
                 WHERE k.project_id = p.id) AS tokens_all,
               (SELECT COUNT(*) FROM session s
                 WHERE s.project_id = p.id AND s.transcript_state = 'live') AS live_sessions,
               (SELECT GROUP_CONCAT(DISTINCT s.tool) FROM session s
                 WHERE s.project_id = p.id) AS tools
        FROM project p {where}
        ORDER BY p.last_seen DESC
    """))

    for proj in projects:
        pid = proj["id"]

        signal = con.execute("""
            SELECT kind, goal, state, next_step, origin, ts FROM progress_signal
            WHERE project_id = ?
              AND (goal IS NOT NULL OR next_step IS NOT NULL OR state IS NOT NULL)
            ORDER BY CASE kind WHEN 'away_summary' THEN 3
                               WHEN 'compact_summary' THEN 2
                               WHEN 'memory_file' THEN 1 ELSE 0 END DESC,
                     ts DESC LIMIT 1
        """, (pid,)).fetchone()
        proj["goal"] = signal["goal"] if signal else None
        proj["next_step"] = signal["next_step"] if signal else None
        proj["state"] = signal["state"] if signal else None

        if not proj["state"]:      # 沒有現成摘要就退回最後一則 assistant 發言
            fallback = con.execute("""
                SELECT assistant_summary FROM turn
                WHERE project_id = ? AND assistant_summary IS NOT NULL
                ORDER BY ts DESC LIMIT 1
            """, (pid,)).fetchone()
            proj["state"] = first_para(fallback["assistant_summary"]) if fallback else None

        proj["light"] = traffic_light(proj)

        last = con.execute("""
            SELECT text, ts FROM prompt WHERE project_id = ? AND is_slash = 0
            ORDER BY ts DESC LIMIT 1
        """, (pid,)).fetchone()
        proj["last_prompt"] = dict(last) if last else None

        proj["sources"] = [r["origin"] for r in con.execute("""
            SELECT origin, COUNT(*) n FROM progress_signal
            WHERE project_id = ? AND origin IS NOT NULL
            GROUP BY origin ORDER BY n DESC LIMIT 4
        """, (pid,))]

        proj["hotspots"] = rows(con.execute("""
            SELECT path, COUNT(*) n FROM file_touch WHERE project_id = ?
            GROUP BY path HAVING n >= 3 ORDER BY n DESC LIMIT 3
        """, (pid,)))

        proj["mismatch"] = mismatch(con, pid, proj["real_path"])

    con.close()
    return {"projects": projects, "appeared": appeared, "vanished": vanished}


@app.get("/api/project/{project_id}")
def project_detail(project_id: int):
    con = db()
    proj = con.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    if proj is None:
        raise HTTPException(404, "no such project")
    sessions = rows(con.execute("""
        SELECT s.id, s.title, s.started_at, s.ended_at, s.prompt_count,
               s.transcript_state, s.tool,
               (SELECT COUNT(DISTINCT path) FROM file_touch f WHERE f.session_id = s.id) AS files,
               (SELECT COUNT(*) FROM commit_ref c WHERE c.session_id = s.id) AS commits,
               (SELECT COUNT(*) FROM turn t WHERE t.session_id = s.id AND t.has_error = 1) AS errors
        FROM session s WHERE s.project_id = ?
        ORDER BY COALESCE(s.started_at, '') DESC
    """, (project_id,)))
    flag = mismatch(con, project_id, proj["real_path"])
    con.close()
    detail = dict(proj)
    detail["light"] = traffic_light(detail)
    detail["mismatch"] = flag
    return {"project": detail, "sessions": sessions}


@app.get("/api/session/{session_id}")
def session_detail(session_id: str):
    con = db()
    sess = con.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
    if sess is None:
        raise HTTPException(404, "no such session")
    payload = {
        "session": dict(sess),
        "prompts": rows(con.execute(
            "SELECT id, ts, seq, text, is_slash, pasted FROM prompt "
            "WHERE session_id = ? ORDER BY ts, seq", (session_id,))),
        "turns": rows(con.execute(
            "SELECT id, ts, assistant_summary, tools_json, has_error FROM turn "
            "WHERE session_id = ? ORDER BY ts", (session_id,))),
        "files": rows(con.execute(
            "SELECT path, verb, COUNT(*) n FROM file_touch WHERE session_id = ? "
            "GROUP BY path, verb ORDER BY n DESC", (session_id,))),
        "commands": rows(con.execute(
            "SELECT ts, command, kind FROM command_run WHERE session_id = ? "
            "ORDER BY ts LIMIT 200", (session_id,))),
        "commits": rows(con.execute(
            "SELECT ts, message FROM commit_ref WHERE session_id = ? ORDER BY ts",
            (session_id,))),
        "signals": rows(con.execute(
            "SELECT kind, goal, state, next_step, origin, ts, "
            "substr(body,1,1200) AS body "
            "FROM progress_signal WHERE session_id = ? ORDER BY ts", (session_id,))),
        "subagents": rows(con.execute(
            "SELECT agent_id, agent_type, description, model, spawn_depth, "
            "turn_count, tool_count, file_count, input_tokens, output_tokens, "
            "started_at, substr(result,1,4000) AS result "
            "FROM subagent WHERE session_id = ? ORDER BY started_at", (session_id,))),
        "usage": session_usage(con, session_id),
    }
    con.close()
    return payload


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), limit: int = 80,
           scope: str = "all", since: str = "", until: str = "",
           slash: str = "all"):
    """搜尋 prompt 與 assistant 回覆。scope: all | prompt | reply | agent

    FTS5 trigram；2 字以下（或 FTS 撲空）自動退回 LIKE。trigram 對 <3 字元
    的查詢是「靜默回 0 筆」而不是報錯，所以 fallback 不是最佳化，是正確性要求。

    since / until 都是 YYYY-MM-DD，until 含當天；slash = all | only | exclude，
    only 時 reply / agent 兩個範圍本來就不可能有東西，直接跳過不查。
    """
    term = q.strip()
    since, until = day_arg(since), day_arg(until)
    if slash not in ("all", "only", "exclude"):
        slash = "all"
    con = db()
    phrase = '"' + term.replace('"', '""') + '"'
    use_fts = len(term) >= 3
    modes = set()

    def cond(ts_expr, slash_col=None):
        """日期 / slash 的 WHERE 片段 -> (SQL, 參數)。空值在這裡擋掉，不進 SQL。"""
        sql, args = "", []
        if since:
            sql += f" AND {ts_expr} >= ?"
            args.append(since)
        if until:                     # 含當天 -> 比到隔天 00:00
            sql += f" AND {ts_expr} < date(?, '+1 day')"
            args.append(until)
        if slash_col and slash != "all":
            sql += f" AND {slash_col} = ?"
            args.append(1 if slash == "only" else 0)
        return sql, tuple(args)

    def fetch(sql_fts, sql_like, where, like_twice=False):
        """FTS 撲空或查詢太短就退回 LIKE。like_twice 給有兩個 LIKE 佔位的查詢。"""
        w, wargs = where
        if use_fts:
            try:
                got = rows(con.execute(sql_fts.format(w=w), (phrase, *wargs, limit)))
                if got:
                    modes.add("fts")
                    return got
            except sqlite3.OperationalError:
                pass
        pattern = like_pattern(term)
        head = (pattern, pattern) if like_twice else (pattern,)
        got = rows(con.execute(sql_like.format(w=w), (*head, *wargs, limit)))
        if got:
            modes.add("like")
        return got

    hits = []
    if scope in ("all", "prompt"):
        hits += fetch("""
            SELECT 'prompt' AS kind, p.id, p.session_id, p.project_id, p.ts,
                   p.text, p.is_slash, pr.display_name, s.title,
                   snippet(prompt_fts, 0, char(2), char(3), '…', 24) AS snip
            FROM prompt_fts f
            JOIN prompt p ON p.id = f.rowid
            LEFT JOIN project pr ON pr.id = p.project_id
            LEFT JOIN session s ON s.id = p.session_id
            WHERE prompt_fts MATCH ?{w} ORDER BY rank LIMIT ?
        """, """
            SELECT 'prompt' AS kind, p.id, p.session_id, p.project_id, p.ts,
                   p.text, p.is_slash, pr.display_name, s.title
            FROM prompt p
            LEFT JOIN project pr ON pr.id = p.project_id
            LEFT JOIN session s ON s.id = p.session_id
            WHERE p.text LIKE ? ESCAPE '\\'{w} ORDER BY p.ts DESC LIMIT ?
        """, cond("p.ts", "p.is_slash"))

    if scope in ("all", "reply") and slash != "only":
        # turn_fts 索引了 2554 筆 assistant 摘要，之前完全沒被查詢過
        hits += fetch("""
            SELECT 'reply' AS kind, t.id, t.session_id, t.project_id, t.ts,
                   t.assistant_summary AS text, 0 AS is_slash,
                   pr.display_name, s.title,
                   snippet(turn_fts, 0, char(2), char(3), '…', 24) AS snip
            FROM turn_fts f
            JOIN turn t ON t.id = f.rowid
            LEFT JOIN project pr ON pr.id = t.project_id
            LEFT JOIN session s ON s.id = t.session_id
            WHERE turn_fts MATCH ?{w} ORDER BY rank LIMIT ?
        """, """
            SELECT 'reply' AS kind, t.id, t.session_id, t.project_id, t.ts,
                   t.assistant_summary AS text, 0 AS is_slash,
                   pr.display_name, s.title
            FROM turn t
            LEFT JOIN project pr ON pr.id = t.project_id
            LEFT JOIN session s ON s.id = t.session_id
            WHERE t.assistant_summary LIKE ? ESCAPE '\\'{w}
            ORDER BY t.ts DESC LIMIT ?
        """, cond("t.ts"))

    if scope in ("all", "agent") and slash != "only":
        # 子代理的回報常常是整份研究結果，主線只留了摘要 —— 值得能搜到
        hits += fetch("""
            SELECT 'agent' AS kind, a.id, a.session_id, a.project_id,
                   COALESCE(a.ended_at, a.started_at) AS ts,
                   COALESCE(a.result, a.description) AS text, 0 AS is_slash,
                   pr.display_name, a.description AS title,
                   snippet(subagent_fts, 1, char(2), char(3), '…', 24) AS snip
            FROM subagent_fts f
            JOIN subagent a ON a.id = f.rowid
            LEFT JOIN project pr ON pr.id = a.project_id
            WHERE subagent_fts MATCH ?{w} ORDER BY rank LIMIT ?
        """, """
            SELECT 'agent' AS kind, a.id, a.session_id, a.project_id,
                   COALESCE(a.ended_at, a.started_at) AS ts,
                   COALESCE(a.result, a.description) AS text, 0 AS is_slash,
                   pr.display_name, a.description AS title
            FROM subagent a
            LEFT JOIN project pr ON pr.id = a.project_id
            WHERE (a.result LIKE ? ESCAPE '\\' OR a.description LIKE ? ESCAPE '\\'){w}
            ORDER BY a.ended_at DESC LIMIT ?
        """, cond("COALESCE(a.ended_at, a.started_at)"), like_twice=True)

    hits.sort(key=lambda h: h["ts"] or "", reverse=True)
    for h in hits:
        # FTS 有 snippet()，LIKE 路徑自己切視窗；兩邊都是「先跳脫再包 mark」
        h["snippet"] = mark_fts(h.pop("snip", None)) or mark_like(h["text"], term)
    con.close()
    return {"mode": "+".join(sorted(modes)) or "none", "query": term,
            "scope": scope, "since": since, "until": until, "slash": slash,
            "count": len(hits),
            "prompts": sum(1 for h in hits if h["kind"] == "prompt"),
            "replies": sum(1 for h in hits if h["kind"] == "reply"),
            "agents": sum(1 for h in hits if h["kind"] == "agent"),
            "hits": hits[:limit * 2]}


@app.get("/api/roots")
def get_roots():
    con = db()
    roots = indexer.get_roots(con)
    con.commit()
    out = [{"path": r, "exists": Path(r).is_dir()} for r in roots]
    con.close()
    return {"roots": out}


@app.post("/api/roots")
def set_roots(payload: dict):
    """整批覆寫根目錄清單。只接受真的存在的目錄。"""
    wanted = payload.get("roots")
    if not isinstance(wanted, list):
        raise HTTPException(400, "roots must be a list")
    bad = [r for r in wanted if not Path(str(r)).is_dir()]
    if bad:
        raise HTTPException(400, f"不是資料夾: {bad[0]}")
    con = db()
    roots = indexer.set_roots(con, wanted)
    indexer.scan_project_dirs(con, indexer.Resolver(con))
    indexer.sync_disk_projects(con)
    indexer.collect_git(con)
    indexer.rollup(con)
    con.commit()
    con.close()
    return {"roots": roots}


def wsl_distros():
    """列出 WSL 發行版，回傳 [(名稱, UNC 路徑)]。非 Windows 或沒裝就回空。"""
    if os.name != "nt":
        return []
    try:
        done = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    raw = done.stdout
    for encoding in ("utf-16-le", "utf-8", "mbcs"):     # wsl.exe 通常吐 UTF-16LE
        try:
            text = raw.decode(encoding)
            if "\x00" not in text:
                break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        return []
    out = []
    for name in (n.strip().strip("﻿") for n in text.splitlines()):
        if not name:
            continue
        unc = f"\\\\wsl.localhost\\{name}"
        if Path(unc).is_dir():
            out.append((name, unc))
    return out


@app.get("/api/browse")
def browse(path: str = ""):
    """列出某個目錄底下的子目錄，給前端的資料夾選擇器用。

    只回目錄名稱與完整路徑，不讀任何檔案內容。
    path 留空時：Windows 回磁碟機 + WSL 發行版，其他系統回根目錄。
    路徑拼接一律在後端做 —— 前端自己接字串在 UNC 路徑上會出錯。
    """
    if not path:
        if os.name == "nt":
            entries = [{"name": f"{c}:", "path": f"{c}:\\"}
                       for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                       if Path(f"{c}:\\").exists()]
            entries += [{"name": f"WSL · {name}", "path": unc}
                        for name, unc in wsl_distros()]
            return {"path": "", "parent": None, "dirs": entries,
                    "home": str(Path.home())}
        path = "/"

    here = Path(path)
    if not here.is_dir():
        raise HTTPException(404, "不是資料夾")
    try:
        dirs = sorted(
            ({"name": p.name, "path": str(p)} for p in here.iterdir()
             if p.is_dir() and not p.name.startswith(".")
             and p.name.lower() not in indexer.SKIP_DIRS),
            key=lambda d: d["name"].lower())
    except OSError as exc:
        raise HTTPException(403, f"讀不到: {exc}")

    parent = here.parent
    # UNC 根（\\wsl.localhost\Ubuntu）再往上沒有意義，回磁碟機/發行版清單
    parent_str = "" if parent == here or str(parent) == str(here) else str(parent)
    return {"path": str(here), "parent": parent_str, "dirs": dirs,
            "home": str(Path.home())}


@app.get("/api/recent")
def recent(limit: int = 60):
    """跨專案的最近動態流。"""
    con = db()
    data = rows(con.execute("""
        SELECT p.id, p.session_id, p.project_id, p.ts, p.text, p.is_slash,
               pr.display_name, s.title
        FROM prompt p
        LEFT JOIN project pr ON pr.id = p.project_id
        LEFT JOIN session s  ON s.id = p.session_id
        WHERE p.is_slash = 0 AND pr.exists_on_disk = 1
        ORDER BY p.ts DESC LIMIT ?
    """, (limit,)))
    con.close()
    return {"hits": data}


@app.get("/api/heatmap")
def heatmap(metric: str = "prompts"):
    """metric=prompts 每天幾則 prompt；metric=tokens 每天燒掉多少 token。

    熱度只算 input + cache_create + output —— cache_read 佔了 97%，拿它當色階
    會把每天壓成同一個顏色，所以只放進 title。有 prompt 卻沒有 api_call 的日子
    仍然要出現（舊 DB 的日子），所以用 UNION ALL 補 0。
    """
    con = db()
    if metric == "tokens":
        data = rows(con.execute("""
            SELECT day, SUM(n) AS n, SUM(out) AS out, SUM(cache_read) AS cache_read
            FROM (
                SELECT substr(ts,1,10) AS day,
                       COALESCE(input_tokens, 0) + COALESCE(cache_create_tokens, 0)
                         + COALESCE(output_tokens, 0) AS n,
                       COALESCE(output_tokens, 0)     AS out,
                       COALESCE(cache_read_tokens, 0) AS cache_read
                FROM api_call WHERE ts IS NOT NULL
                UNION ALL
                SELECT substr(ts,1,10), 0, 0, 0 FROM prompt
            ) GROUP BY day ORDER BY day
        """))
    else:
        metric = "prompts"
        data = rows(con.execute(
            "SELECT substr(ts,1,10) AS day, COUNT(*) n FROM prompt GROUP BY day ORDER BY day"))
    con.close()
    return {"metric": metric, "days": data}


@app.get("/api/day/{day}")
def day_detail(day: str):
    con = db()
    data = rows(con.execute("""
        SELECT p.id, p.session_id, p.project_id, p.ts, p.text, p.is_slash,
               pr.display_name, s.title
        FROM prompt p
        LEFT JOIN project pr ON pr.id = p.project_id
        LEFT JOIN session s  ON s.id = p.session_id
        WHERE substr(p.ts,1,10) = ? ORDER BY p.ts
    """, (day,)))
    con.close()
    return {"day": day, "hits": data}


@app.post("/api/reindex")
def reindex():
    # 使用者按 ↻ 的語意就是「現在給我最新的」，所以 git 狀態不吃快取
    return indexer.run(full=False, force_git=True)


# @F1-api

def session_usage(con, session_id):
    """這個 session 燒掉多少 token。api_call 已依 request_id 去重。"""
    total = dict(con.execute("""
        SELECT COUNT(*) AS calls,
               COALESCE(SUM(input_tokens), 0)        AS input,
               COALESCE(SUM(cache_create_tokens), 0) AS cache_create,
               COALESCE(SUM(cache_read_tokens), 0)   AS cache_read,
               COALESCE(SUM(output_tokens), 0)       AS output,
               COALESCE(SUM(thinking_tokens), 0)     AS thinking
        FROM api_call WHERE session_id = ?
    """, (session_id,)).fetchone())
    total["models"] = rows(con.execute("""
        SELECT model, COUNT(*) AS calls,
               COALESCE(SUM(output_tokens), 0) AS output,
               COALESCE(SUM(input_tokens + cache_create_tokens), 0) AS input_all
        FROM api_call WHERE session_id = ?
        GROUP BY model ORDER BY output DESC
    """, (session_id,)))
    return total


@app.get("/api/project/{project_id}/tokens")
def project_tokens(project_id: int):
    """錢花到哪去了：總量、模型分佈、每日曲線，以及子代理燒掉的部分。"""
    con = db()
    if con.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
        con.close()
        raise HTTPException(404, "no such project")
    total = dict(con.execute("""
        SELECT COUNT(*) AS calls,
               COALESCE(SUM(input_tokens), 0)        AS input,
               COALESCE(SUM(cache_create_tokens), 0) AS cache_create,
               COALESCE(SUM(cache_read_tokens), 0)   AS cache_read,
               COALESCE(SUM(output_tokens), 0)       AS output,
               COALESCE(SUM(thinking_tokens), 0)     AS thinking
        FROM api_call WHERE project_id = ?
    """, (project_id,)).fetchone())
    models = rows(con.execute("""
        SELECT model, COUNT(*) AS calls,
               COALESCE(SUM(output_tokens), 0) AS output,
               COALESCE(SUM(input_tokens + cache_create_tokens), 0) AS input_all
        FROM api_call WHERE project_id = ?
        GROUP BY model ORDER BY output + input_all DESC
    """, (project_id,)))
    by_day = rows(con.execute("""
        SELECT substr(ts,1,10) AS day,
               COALESCE(SUM(output_tokens), 0) AS output,
               COALESCE(SUM(input_tokens + cache_create_tokens), 0) AS input_all
        FROM api_call WHERE project_id = ? AND ts IS NOT NULL
        GROUP BY day ORDER BY day
    """, (project_id,)))
    agents = dict(con.execute("""
        SELECT COALESCE(SUM(output_tokens), 0) AS output,
               COALESCE(SUM(input_tokens), 0)  AS input
        FROM subagent WHERE project_id = ?
    """, (project_id,)).fetchone())
    con.close()
    return {"total": total, "models": models, "by_day": by_day, "agents": agents}


# @F2-api

def rel_to_project(path, root):
    """去掉專案前綴。Windows 上不分大小寫，`\\` 與 `/` 一視同仁。

    同一個檔在紀錄裡可能寫成 `C:\\work\\a.py` 或 `c:/work/a.py`，兩種都要能
    對上專案根目錄。正規化只做「換字元」與「轉小寫」，長度不變，所以可以拿
    正規化後的長度回頭切原字串，切出來的 rel 保留原本的大小寫與分隔符。
    去不掉就回原路徑。
    """
    if not root:
        return path

    def norm(s):
        s = s.replace("\\", "/")
        return s.lower() if os.name == "nt" else s

    base = norm(root).rstrip("/")
    if base and norm(path).startswith(base + "/"):
        return path[len(base) + 1:]
    return path


@app.get("/api/project/{project_id}/files")
def project_files(project_id: int, limit: int = Query(60, ge=1, le=500)):
    """這個專案被改最多次的檔案。子代理改的也算，但另外記在 agent_n。"""
    con = db()
    proj = con.execute(
        "SELECT real_path FROM project WHERE id = ?", (project_id,)).fetchone()
    if proj is None:
        con.close()
        raise HTTPException(404, "no such project")
    # Windows 上同一個檔可能有 C:\ 與 c:\ 兩種寫法，合併計次才不會拆成兩筆
    group_key = "lower(path)" if os.name == "nt" else "path"
    files = rows(con.execute(f"""
        SELECT MIN(path) AS path, COUNT(*) AS n,
               SUM(verb = 'Edit')          AS edits,
               SUM(verb = 'Write')         AS writes,
               SUM(via_agent IS NOT NULL)  AS agent_n,
               COUNT(DISTINCT session_id)  AS sessions,
               MAX(ts)                     AS last_ts
        FROM file_touch WHERE project_id = ?
        GROUP BY {group_key} ORDER BY n DESC, last_ts DESC LIMIT ?
    """, (project_id, limit)))
    con.close()
    root = proj["real_path"]
    for f in files:
        f["rel"] = rel_to_project(f["path"], root)
    return {"root": root, "files": files}


@app.get("/api/project/{project_id}/file")
def project_file(project_id: int, path: str = Query(..., min_length=1)):
    """這個檔案被哪些 session 改過。path 是 /files 回的原始路徑（含反斜線）。"""
    con = db()
    if con.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
        con.close()
        raise HTTPException(404, "no such project")
    match = "lower(f.path) = lower(?)" if os.name == "nt" else "f.path = ?"
    sessions = rows(con.execute(f"""
        SELECT s.id, s.title, s.started_at, COUNT(*) AS n,
               GROUP_CONCAT(DISTINCT f.verb)   AS verbs,
               SUM(f.via_agent IS NOT NULL)    AS via_agent_n
        FROM file_touch f JOIN session s ON s.id = f.session_id
        WHERE f.project_id = ? AND {match}
        GROUP BY s.id ORDER BY COALESCE(s.started_at, '') DESC
    """, (project_id, path)))
    con.close()
    return {"path": path, "sessions": sessions}


# @F3-api

@app.get("/api/project/{project_id}/timeline")
def project_timeline(project_id: int, limit: int = Query(200, ge=1, le=1000)):
    """這個專案的目標一路怎麼變的 —— 只有「會講目標的」訊號進得來。

    `last_prompt` 只是最後一句話、`cost_state` 是花費統計，兩個都沒有目標可言，
    放進來只會把真正換過方向的那幾天洗掉。
    """
    con = db()
    if con.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
        con.close()
        raise HTTPException(404, "no such project")
    # 超過 limit 時砍最舊的，不是最新的：先取最新 N 筆再反轉成由舊到新
    items = rows(con.execute("""
        SELECT g.ts, g.kind, g.origin, g.session_id, s.title,
               g.goal, g.state, g.next_step
        FROM progress_signal g LEFT JOIN session s ON s.id = g.session_id
        WHERE g.project_id = ?
          AND g.kind IN ('away_summary', 'compact_summary', 'memory_file')
          AND (g.goal IS NOT NULL OR g.state IS NOT NULL OR g.next_step IS NOT NULL)
        ORDER BY g.ts DESC LIMIT ?
    """, (project_id, limit)))
    con.close()
    items.reverse()
    return {"items": items}


# @F4-api

import parser as P

MISMATCH_COLD_DAYS = 30       # 進度檔提到的檔案，幾天沒被碰就算「寫了沒做」
MISMATCH_STALE_DAYS = 14      # 進度檔比實作落後幾天算沒跟上
MISMATCH_STALE_TOUCHES = 10   # 落後期間至少改幾次檔，才不是「整個專案都停著」


def _iso_day(ts):
    """UTC ISO 字串 -> date。只取前 10 碼。

    memory 的 ts 是 mtime 轉出來的 `…+00:00`，transcript 的是 `…Z`，兩種都是
    UTC，但尾巴不同 —— 直接比字串只有日期部分靠得住，那也正好是這裡要的粒度。
    """
    try:
        return dt.date.fromisoformat(str(ts)[:10])
    except (TypeError, ValueError):
        return None


def _touch_matches(touch_path, candidate):
    """file_touch 的完整路徑是不是就是進度檔寫的那個候選。

    手寫進度檔幾乎不寫絕對路徑（本機樣本：`診所POS/docs/系統規劃.md`），
    所以一律用結尾比對，並且要對在分隔線上 —— 不然 `config.py` 會吃到
    `myconfig.py`。
    """
    norm = touch_path.replace("\\", "/").lower()
    return norm == candidate or norm.endswith("/" + candidate)


def mismatch(con, project_id, real_path=None, now=None):
    """手寫的進度檔與實際改檔歷史對不上 -> 旗標，對得上 -> None。

    回 `{"kind", "detail", "files", "memory_ts"}`，kind 是
    `mentioned_untouched`（寫進進度檔的檔案被丟在後面）或
    `stale_memory`（進度檔整份過期）。兩個都成立時報前者：它點得出檔名，
    比「你的 brain.md 舊了」可行動得多。

    基準時間 `now`：沒給的話用「這個專案最後一次 file_touch 的日期」，不是系統
    時鐘。用系統時鐘的話，一個停擺一年的專案會把進度檔裡每個檔案都標紅，那講的
    是「沒人動這個專案」不是「手寫與實作不符」；用專案自己的時鐘，旗標的意思才
    會是「實作一直在前進，但這個檔案被留在原地」。測試也才不會隨著日子變紅。
    """
    sig = con.execute("""
        SELECT ts, goal, next_step, body, origin FROM progress_signal
        WHERE project_id = ? AND kind = 'memory_file' AND body IS NOT NULL
        ORDER BY ts DESC LIMIT 1
    """, (project_id,)).fetchone()
    if sig is None:
        return None
    memory_day = _iso_day(sig["ts"])
    if memory_day is None:
        return None

    touches = [t for t in con.execute("""
        SELECT path, MAX(ts) AS last_ts FROM file_touch
        WHERE project_id = ? AND ts IS NOT NULL GROUP BY path
    """, (project_id,)) if _iso_day(t["last_ts"])]
    if not touches:
        return None

    latest_day = max(_iso_day(t["last_ts"]) for t in touches)
    ref_day = (_iso_day(now) or latest_day) if now else latest_day
    origin = sig["origin"] or "手寫進度檔"

    # 1) 進度檔還在講、實作卻早就放著沒動的檔案
    if sig["goal"] or sig["next_step"]:
        cold = []
        for cand in P.memory_paths(sig["body"]):
            hits = [t for t in touches if _touch_matches(t["path"], cand)]
            if not hits:
                continue          # 這個專案從來沒碰過 -> 不是專案檔，不算
            newest = max(hits, key=lambda t: t["last_ts"])
            day = _iso_day(newest["last_ts"])
            if (ref_day - day).days >= MISMATCH_COLD_DAYS:
                cold.append((day, newest["path"]))
        if cold:
            cold.sort()
            names = [rel_to_project(p, real_path).replace("\\", "/") for _, p in cold]
            shown = "、".join(names[:3]) + (f" 等 {len(names)} 個" if len(names) > 3 else "")
            return {"kind": "mentioned_untouched",
                    "detail": f"{origin} 提到 {shown}，{MISMATCH_COLD_DAYS} 天內沒改過",
                    "files": [p for _, p in cold], "memory_ts": sig["ts"]}

    # 2) 進度檔整份落後：實作一路往前，手寫的那份停在很久以前
    gap = (latest_day - memory_day).days
    if gap >= MISMATCH_STALE_DAYS:
        since = con.execute(
            "SELECT COUNT(*) FROM file_touch WHERE project_id = ? AND ts > ?",
            (project_id, sig["ts"])).fetchone()[0]
        if since >= MISMATCH_STALE_TOUCHES:
            return {"kind": "stale_memory",
                    "detail": f"{origin} 已 {gap} 天沒更新，期間改了 {since} 次檔",
                    "files": [], "memory_ts": sig["ts"]}
    return None


# @F5-api


# @F6-api
import export
from fastapi import Response


@app.get("/api/session/{session_id}/export.md")
def session_export(session_id: str):
    """整份 session 的 Markdown，給交接文件用。404 沿用 session_detail。"""
    payload = session_detail(session_id)
    con = db()
    proj = con.execute("SELECT real_path FROM project WHERE id = ?",
                       (payload["session"].get("project_id"),)).fetchone()
    con.close()
    payload["project"] = dict(proj) if proj else None
    md = export.session_markdown(payload)
    # 檔名只用 id 前 8 碼且濾成 ASCII：Content-Disposition 非 ASCII 會炸
    stem = "".join(c for c in session_id[:8]
                   if c.isascii() and (c.isalnum() or c in "-_")) or "session"
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="session-{stem}.md"'},
    )


# @F7-api


# @F8-api
# F8 沒有新端點，只有 search() 用得到的小工具。
import html
import re

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SNIP_OPEN, SNIP_CLOSE = "\x02", "\x03"


def day_arg(value):
    """YYYY-MM-DD 才算數；空字串與亂寫的一律當沒給，不要丟進 SQL 的 date()。

    只比對形狀不夠：2026-13-45 過得了 regex，SQLite date() 回 NULL，
    結果是靜默 0 筆還把假日期原樣 echo 回去。"""
    value = (value or "").strip()
    if not DAY_RE.match(value):
        return ""
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return ""
    return value


def esc(text):
    """跟前端 esc() 同一套：只跳脫 & < >。"""
    return html.escape(text, quote=False)


def mark_fts(snip):
    """snippet() 的 \\x02 / \\x03 標記換成 <mark>。

    順序不能反：先跳脫整段，再換標記。反過來的話 <mark> 自己會被 esc 吃掉。
    """
    if not snip or SNIP_OPEN not in snip:
        return None      # 命中在別的欄位（子代理的 description）-> 讓呼叫端退回去
    return esc(snip).replace(SNIP_OPEN, "<mark>").replace(SNIP_CLOSE, "</mark>")


def mark_like(text, term, window=60):
    """LIKE 路徑沒有 snippet()，自己切一段前後 window 字的視窗再包 mark。"""
    if not text or not term:
        return None
    pat = re.compile(re.escape(term), re.I)
    first = pat.search(text)
    if first is None:                # 例如子代理只有 description 命中
        return None
    at = first.start()
    start, end = max(0, at - window), min(len(text), at + len(term) + window)
    chunk = text[start:end]
    # 在原文上找命中、逐段跳脫再接回去。若先 esc 再找，q=lt / amp 會把
    # &lt; 這種實體內部也包進 mark，畫面上就出現字面的 "&lt;"。
    out, pos = [], 0
    for m in pat.finditer(chunk):
        out.append(esc(chunk[pos:m.start()]))
        out.append(f"<mark>{esc(m.group(0))}</mark>")
        pos = m.end()
    out.append(esc(chunk[pos:]))
    return ("…" if start else "") + "".join(out) + ("…" if end < len(text) else "")


# @F9-api

ZERO_TOKENS = {"calls": 0, "output": 0, "input_all": 0, "cache_read": 0}


def week_bounds(start):
    """(週一, 下週一)。start 空字串 -> 本地時間的本週一；格式錯 -> 400。"""
    if not start:
        today = dt.date.today()
        begin = today - dt.timedelta(days=today.weekday())
    else:
        try:
            begin = dt.date.fromisoformat(start)
        except ValueError:
            raise HTTPException(400, "start 要是 YYYY-MM-DD")
    return begin.isoformat(), (begin + dt.timedelta(days=7)).isoformat()


def week_tokens(con, start, end, per_project=False):
    """這段期間燒掉的 token。api_call 是 F1 才有的表，舊 DB 沒有就當成沒用量。"""
    sql = f"""
        SELECT {"project_id, " if per_project else ""}COUNT(*) AS calls,
               COALESCE(SUM(output_tokens), 0)                      AS output,
               COALESCE(SUM(input_tokens + cache_create_tokens), 0) AS input_all,
               COALESCE(SUM(cache_read_tokens), 0)                  AS cache_read
        FROM api_call WHERE ts >= ? AND ts < ?
        {"GROUP BY project_id" if per_project else ""}
    """
    try:
        got = rows(con.execute(sql, (start, end)))
    except sqlite3.OperationalError as exc:
        # 只吞「表還沒建」（換了新程式碼但還沒跑過索引）；其他 SQL 錯誤照常炸出來
        if "no such table" not in str(exc):
            raise
        return [] if per_project else dict(ZERO_TOKENS)
    return got if per_project else (got[0] if got else dict(ZERO_TOKENS))


def week_totals(con, start, end):
    """給 prev 對照用的三個數字。"""
    prompts = con.execute(
        "SELECT COUNT(*) FROM prompt WHERE is_slash = 0 AND ts >= ? AND ts < ?",
        (start, end)).fetchone()[0]
    commits = con.execute(
        "SELECT COUNT(*) FROM commit_ref WHERE ts >= ? AND ts < ?",
        (start, end)).fetchone()[0]
    return {"prompts": prompts, "commits": commits,
            "tokens_out": week_tokens(con, start, end)["output"]}


@app.get("/api/week")
def week(start: str = ""):
    """這週跨專案發生了什麼。

    所有 ts 都是 UTC 的 ISO 字串，比較就直接 `>= start AND < end`（與 heatmap
    同一套）—— 本地時間的週一 00:00 不等於 UTC 的週一 00:00，前端要註明。
    """
    start, end = week_bounds(start)
    prev_start = (dt.date.fromisoformat(start) - dt.timedelta(days=7)).isoformat()
    con = db()
    args = (start, end)

    agg = {}

    def bucket(pid):
        return agg.setdefault(pid, {
            "id": pid, "display_name": None, "prompts": 0, "sessions": 0,
            "files": 0, "commits": 0, "tokens_out": 0, "tokens_all": 0,
            "goal": None, "next_step": None})

    for r in rows(con.execute("""
        SELECT project_id, COUNT(*) AS n FROM prompt
        WHERE is_slash = 0 AND ts >= ? AND ts < ? AND project_id IS NOT NULL
        GROUP BY project_id""", args)):
        bucket(r["project_id"])["prompts"] = r["n"]

    # session 數要含「這週只有 commit / 改檔」的專案，不能只看 prompt
    for r in rows(con.execute("""
        SELECT project_id, COUNT(DISTINCT session_id) AS n FROM (
            SELECT project_id, session_id FROM prompt      WHERE ts >= ? AND ts < ?
            UNION
            SELECT project_id, session_id FROM commit_ref  WHERE ts >= ? AND ts < ?
            UNION
            SELECT project_id, session_id FROM file_touch  WHERE ts >= ? AND ts < ?
        ) WHERE project_id IS NOT NULL GROUP BY project_id""", args * 3)):
        bucket(r["project_id"])["sessions"] = r["n"]

    for r in rows(con.execute("""
        SELECT project_id, COUNT(DISTINCT path) AS n FROM file_touch
        WHERE ts >= ? AND ts < ? AND project_id IS NOT NULL
        GROUP BY project_id""", args)):
        bucket(r["project_id"])["files"] = r["n"]

    for r in rows(con.execute("""
        SELECT project_id, COUNT(*) AS n FROM commit_ref
        WHERE ts >= ? AND ts < ? AND project_id IS NOT NULL
        GROUP BY project_id""", args)):
        bucket(r["project_id"])["commits"] = r["n"]

    for r in week_tokens(con, start, end, per_project=True):
        if r["project_id"] is None:
            continue
        b = bucket(r["project_id"])
        b["tokens_out"] = r["output"]
        b["tokens_all"] = r["input_all"] + r["output"]

    # 這週最新的進度訊號（ts 由舊到新，後面的覆蓋前面的 —— 但逐欄覆蓋，
    # 只有 goal 的新訊號不該把前一筆的 next_step 洗成 null）
    for r in rows(con.execute("""
        SELECT project_id, goal, next_step FROM progress_signal
        WHERE ts >= ? AND ts < ? AND project_id IS NOT NULL
          AND (goal IS NOT NULL OR next_step IS NOT NULL)
        ORDER BY ts""", args)):
        b = bucket(r["project_id"])
        if r["goal"]:
            b["goal"] = r["goal"]
        if r["next_step"]:
            b["next_step"] = r["next_step"]

    for r in con.execute("SELECT id, display_name FROM project"):
        if r["id"] in agg:
            agg[r["id"]]["display_name"] = r["display_name"]

    projects = sorted(agg.values(),
                      key=lambda p: (-p["prompts"], -p["commits"],
                                     p["display_name"] or ""))

    commits = rows(con.execute("""
        SELECT c.project_id, pr.display_name, c.ts, c.message
        FROM commit_ref c LEFT JOIN project pr ON pr.id = c.project_id
        WHERE c.ts >= ? AND c.ts < ? ORDER BY c.ts""", args))

    # 同一個檔案一週內被改 3 次以上 —— 大概是在原地打轉
    flags = rows(con.execute("""
        SELECT f.project_id, pr.display_name, f.path, COUNT(*) AS n
        FROM file_touch f LEFT JOIN project pr ON pr.id = f.project_id
        WHERE f.ts >= ? AND f.ts < ?
        GROUP BY f.project_id, f.path HAVING n >= 3 ORDER BY n DESC""", args))

    payload = {"start": start, "end": end, "projects": projects,
               "commits": commits, "flags": flags,
               "tokens": week_tokens(con, start, end),
               "prev": week_totals(con, prev_start, start)}
    con.close()
    return payload


if __name__ == "__main__":
    import uvicorn

    if not indexer.DB_PATH.exists():
        print("首次啟動，建立索引…")
        indexer.run(full=True)

    url = f"http://127.0.0.1:{PORT}"
    print(f"CLI History Visualizer -> {url}")
    webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
