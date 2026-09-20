"""CLI History Visualizer — 本機 server。

    python server.py      ->  http://127.0.0.1:8787

每次要專案清單時會順手 stat 一次資料夾（sync_only），所以資料夾刪掉、
放回來都會即時反映在網頁上。完整重新索引走 POST /api/reindex。
"""

from __future__ import annotations

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
    con.close()
    detail = dict(proj)
    detail["light"] = traffic_light(detail)
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
    }
    con.close()
    return payload


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), limit: int = 80,
           scope: str = "all"):
    """搜尋 prompt 與 assistant 回覆。scope: all | prompt | reply

    FTS5 trigram；2 字以下（或 FTS 撲空）自動退回 LIKE。trigram 對 <3 字元
    的查詢是「靜默回 0 筆」而不是報錯，所以 fallback 不是最佳化，是正確性要求。
    """
    term = q.strip()
    con = db()
    phrase = '"' + term.replace('"', '""') + '"'
    use_fts = len(term) >= 3
    modes = set()

    def fetch(sql_fts, sql_like, params_extra=()):
        if use_fts:
            try:
                got = rows(con.execute(sql_fts, (phrase, limit, *params_extra)))
                if got:
                    modes.add("fts")
                    return got
            except sqlite3.OperationalError:
                pass
        got = rows(con.execute(sql_like, (like_pattern(term), limit, *params_extra)))
        if got:
            modes.add("like")
        return got

    hits = []
    if scope in ("all", "prompt"):
        hits += fetch("""
            SELECT 'prompt' AS kind, p.id, p.session_id, p.project_id, p.ts,
                   p.text, p.is_slash, pr.display_name, s.title
            FROM prompt_fts f
            JOIN prompt p ON p.id = f.rowid
            LEFT JOIN project pr ON pr.id = p.project_id
            LEFT JOIN session s ON s.id = p.session_id
            WHERE prompt_fts MATCH ? ORDER BY rank LIMIT ?
        """, """
            SELECT 'prompt' AS kind, p.id, p.session_id, p.project_id, p.ts,
                   p.text, p.is_slash, pr.display_name, s.title
            FROM prompt p
            LEFT JOIN project pr ON pr.id = p.project_id
            LEFT JOIN session s ON s.id = p.session_id
            WHERE p.text LIKE ? ESCAPE '\\' ORDER BY p.ts DESC LIMIT ?
        """)

    if scope in ("all", "reply"):
        # turn_fts 索引了 2554 筆 assistant 摘要，之前完全沒被查詢過
        hits += fetch("""
            SELECT 'reply' AS kind, t.id, t.session_id, t.project_id, t.ts,
                   t.assistant_summary AS text, 0 AS is_slash,
                   pr.display_name, s.title
            FROM turn_fts f
            JOIN turn t ON t.id = f.rowid
            LEFT JOIN project pr ON pr.id = t.project_id
            LEFT JOIN session s ON s.id = t.session_id
            WHERE turn_fts MATCH ? ORDER BY rank LIMIT ?
        """, """
            SELECT 'reply' AS kind, t.id, t.session_id, t.project_id, t.ts,
                   t.assistant_summary AS text, 0 AS is_slash,
                   pr.display_name, s.title
            FROM turn t
            LEFT JOIN project pr ON pr.id = t.project_id
            LEFT JOIN session s ON s.id = t.session_id
            WHERE t.assistant_summary LIKE ? ESCAPE '\\'
            ORDER BY t.ts DESC LIMIT ?
        """)

    hits.sort(key=lambda h: h["ts"] or "", reverse=True)
    con.close()
    return {"mode": "+".join(sorted(modes)) or "none", "query": term,
            "scope": scope, "count": len(hits),
            "prompts": sum(1 for h in hits if h["kind"] == "prompt"),
            "replies": sum(1 for h in hits if h["kind"] == "reply"),
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
def heatmap():
    con = db()
    data = rows(con.execute(
        "SELECT substr(ts,1,10) AS day, COUNT(*) n FROM prompt GROUP BY day ORDER BY day"))
    con.close()
    return {"days": data}


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
    return indexer.run(full=False)


if __name__ == "__main__":
    import uvicorn

    if not indexer.DB_PATH.exists():
        print("首次啟動，建立索引…")
        indexer.run(full=True)

    url = f"http://127.0.0.1:{PORT}"
    print(f"CLI History Visualizer -> {url}")
    webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
