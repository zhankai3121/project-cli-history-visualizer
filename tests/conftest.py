"""測試用的合成 ~/.claude 樹。

刻意完全不碰使用者真實的 ~/.claude —— 測試必須在任何人的機器上都能跑，
而且不能因為某人的歷史資料剛好長得不一樣就紅燈。
"""

import datetime as dt
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def user_prompt(text, uuid="u1", ts="2026-09-01T10:00:00.000Z"):
    """會被判定為真人輸入的記錄。"""
    return {"type": "user", "uuid": uuid, "parentUuid": None, "timestamp": ts,
            "sessionId": "sess-1", "cwd": None, "version": "2.1.270",
            "origin": {"kind": "human"}, "promptSource": "typed",
            "message": {"role": "user", "content": text}}


def assistant(text, tools=(), ts="2026-09-01T10:00:05.000Z"):
    content = [{"type": "text", "text": text}]
    for name, params in tools:
        content.append({"type": "tool_use", "id": f"toolu_{name}",
                        "name": name, "input": params})
    return {"type": "assistant", "uuid": "a1", "timestamp": ts,
            "sessionId": "sess-1", "version": "2.1.270",
            "message": {"role": "assistant", "content": content}}


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """建一個假的 ~/.claude，並把 indexer 的所有路徑常數指過去。"""
    import gemini
    import indexer

    claude = tmp_path / ".claude"
    projects = claude / "projects"
    work = tmp_path / "work"                 # 假的專案根目錄
    (work / "alpha").mkdir(parents=True)
    (work / "beta").mkdir(parents=True)
    (work / "alpha" / "src").mkdir()         # 深度 2、沒有 .git -> 不算專案

    jsonl(claude / "history.jsonl", [
        {"display": "第一個問題", "timestamp": 1788000000000,
         "project": str(work / "alpha"), "sessionId": "sess-1"},
        {"display": "/effort", "timestamp": 1788000060000,
         "project": str(work / "alpha"), "sessionId": "sess-1"},
        {"display": "部署到正式機", "timestamp": 1788000120000,
         "project": str(work / "beta"), "sessionId": "sess-2"},
    ])

    jsonl(projects / "slug-alpha" / "sess-1.jsonl", [
        {"type": "session_start", "cwd": str(work / "alpha"),
         "sessionId": "sess-1", "timestamp": "2026-09-01T09:59:00.000Z"},
        user_prompt("第一個問題"),
        assistant("我先看一下設定檔。", tools=[
            ("Edit", {"file_path": str(work / "alpha" / "config.py")}),
            ("Bash", {"command": "git commit -F - <<'EOF'\nfeat: 加上設定\nEOF"}),
        ]),
        {"type": "ai-title", "aiTitle": "設定檔調整", "sessionId": "sess-1"},
        {"type": "system", "subtype": "away_summary",
         "sessionId": "sess-1", "timestamp": "2026-09-01T10:30:00.000Z",
         "content": "Goal: ship the config change. Next: run the tests."},
    ])

    # 使用者手寫的進度檔（對照欄）。它的訊號時間就是檔案 mtime，所以固定住 ——
    # 不然每次跑測試都會落在「本週」，每週回顧那幾條會跟著飄。
    # 刻意放在 away_summary（09-01）之前一週：/api/week 是同週內後到覆蓋，
    # 若跟 away_summary 同週且時間較晚，test_week 的 goal 斷言會被這份手寫檔蓋掉。
    brain = projects / "slug-alpha" / "memory" / "brain.md"
    brain.parent.mkdir(parents=True, exist_ok=True)
    brain.write_text("## Focus\n把設定搬進 config.py\n\n"
                     "## Next (when resuming)\n補上測試\n", encoding="utf-8")
    stamp = dt.datetime(2026, 8, 25, 9, 0, tzinfo=dt.timezone.utc).timestamp()
    os.utime(brain, (stamp, stamp))

    monkeypatch.setattr(indexer, "CLAUDE_DIR", claude)
    monkeypatch.setattr(indexer, "HISTORY", claude / "history.jsonl")
    monkeypatch.setattr(indexer, "PROJECTS", projects)
    monkeypatch.setattr(indexer, "PASTE_CACHE", claude / "paste-cache")
    monkeypatch.setattr(indexer, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(indexer, "BACKUP_DIR", tmp_path / "backup")
    monkeypatch.setattr(indexer, "DEFAULT_ROOT", work)
    # 真實的 ~/.gemini 可能有資料，indexer.run() 會掃到它 —— 一律指到 tmp_path
    # 底下（預設不存在＝沒裝）。要測 Gemini 的自己在這個路徑建樹。
    monkeypatch.setattr(gemini, "GEMINI_HOME", tmp_path / ".gemini")
    return {"claude": claude, "projects": projects, "work": work,
            "gemini": tmp_path / ".gemini", "db": tmp_path / "index.db"}


@pytest.fixture
def indexed(fake_home):
    import indexer
    indexer.run(full=True)
    return fake_home


@pytest.fixture
def client(indexed):
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)
