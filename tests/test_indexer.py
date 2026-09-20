"""索引管線：跑在合成的 ~/.claude 上，不碰使用者真實資料。"""

import json
import sqlite3

import pytest

import indexer
from conftest import jsonl, user_prompt, assistant


def rows(db, sql, *args):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return out


def one(db, sql, *args):
    return rows(db, sql, *args)[0]["v"]


def test_history_進了_prompt_表(indexed):
    db = indexed["db"]
    assert one(db, "SELECT COUNT(*) v FROM prompt") == 3
    assert one(db, "SELECT COUNT(*) v FROM prompt WHERE is_slash = 1") == 1


def test_專案用真實路徑不是_slug(indexed):
    paths = [r["real_path"] for r in rows(indexed["db"], "SELECT real_path FROM project")]
    assert str(indexed["work"] / "alpha") in paths
    assert not any("slug-" in p for p in paths)


def test_在根目錄下跑過_CLI_會標成容器(fake_home):
    """根目錄不是專案。在那裡直接跑 CLI 會產生看起來像專案的雜訊卡片。"""
    work = fake_home["work"]
    jsonl(fake_home["claude"] / "history.jsonl", [
        {"display": "在根目錄問的問題", "timestamp": 1788000000000,
         "project": str(work), "sessionId": "s-root"},
        {"display": "在子專案問的", "timestamp": 1788000001000,
         "project": str(work / "alpha"), "sessionId": "s-alpha"},
    ])
    indexer.run(full=True)
    db = fake_home["db"]
    assert one(db, "SELECT is_container v FROM project WHERE real_path = ?",
               str(work)) == 1
    assert one(db, "SELECT is_container v FROM project WHERE real_path = ?",
               str(work / "alpha")) == 0


def test_資料夾掃描有登記子專案(indexed):
    r = one(indexed["db"], "SELECT is_scanned v FROM project WHERE real_path = ?",
            str(indexed["work"] / "beta"))
    assert r == 1


def test_深度2沒有_git_的資料夾不算專案(indexed):
    """alpha/src 不該被當成專案。"""
    hit = rows(indexed["db"], "SELECT 1 FROM project WHERE real_path LIKE ?",
               "%" + "src")
    assert hit == []


def test_transcript_抽出改檔與_commit(indexed):
    db = indexed["db"]
    assert one(db, "SELECT COUNT(*) v FROM file_touch") == 1
    assert one(db, "SELECT COUNT(*) v FROM commit_ref") == 1
    assert one(db, "SELECT message v FROM commit_ref") == "feat: 加上設定"


def test_away_summary_進了進度訊號(indexed):
    r = one(indexed["db"],
            "SELECT goal v FROM progress_signal WHERE kind = 'away_summary'")
    assert r == "ship the config change."


def test_ai_title_當成_session_標題(indexed):
    assert one(indexed["db"], "SELECT title v FROM session WHERE id = 'sess-1'") \
        == "設定檔調整"


def test_沒有_transcript_的_session_標成_gone(indexed):
    assert one(indexed["db"],
               "SELECT transcript_state v FROM session WHERE id = 'sess-2'") == "gone"


def test_增量索引不會重複插入(indexed):
    before = one(indexed["db"], "SELECT COUNT(*) v FROM prompt")
    indexer.run(full=False)
    assert one(indexed["db"], "SELECT COUNT(*) v FROM prompt") == before


def test_full_重建會保留根目錄設定(indexed):
    db = indexed["db"]
    con = sqlite3.connect(db)
    indexer.set_roots(con, [str(indexed["work"]), str(indexed["work"] / "alpha")])
    con.commit()
    con.close()

    indexer.run(full=True)

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    got = indexer.get_roots(con)
    con.close()
    assert len(got) == 2


def test_移除根目錄會清掉只靠掃描來的專案(indexed):
    db = indexed["db"]
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    # beta 有 CLI 紀錄，alpha 也有；另外造一個純掃描來的
    (indexed["work"] / "gamma").mkdir()
    indexer.set_roots(con, [str(indexed["work"])])
    indexer.scan_project_dirs(con, indexer.Resolver(con))
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM project WHERE real_path = ?",
                       (str(indexed["work"] / "gamma"),)).fetchone()[0] == 1

    # 把根目錄清空 -> gamma 沒有任何資料，應該被刪
    indexer.set_roots(con, [])
    added, orphans = indexer.scan_project_dirs(con, indexer.Resolver(con))
    con.commit()
    assert orphans >= 1
    assert con.execute("SELECT COUNT(*) FROM project WHERE real_path = ?",
                       (str(indexed["work"] / "gamma"),)).fetchone()[0] == 0

    # 有 CLI 紀錄的不能被刪
    assert con.execute("SELECT COUNT(*) FROM project WHERE real_path = ?",
                       (str(indexed["work"] / "alpha"),)).fetchone()[0] == 1
    con.close()


def test_中文子目錄不會因為_slug_撞名而合併(fake_home):
    """slug 把非 ASCII 逐字轉 '-'，不同中文目錄會產生相同 slug。"""
    work = fake_home["work"]
    for name in ("完整規劃", "問題規劃", "套版整合"):
        (work / name).mkdir()
    jsonl(fake_home["claude"] / "history.jsonl", [
        {"display": f"在 {n} 工作", "timestamp": 1788000000000 + i * 1000,
         "project": str(work / n), "sessionId": f"s-{i}"}
        for i, n in enumerate(("完整規劃", "問題規劃", "套版整合"))
    ])
    indexer.run(full=True)
    paths = [r["real_path"] for r in
             rows(fake_home["db"], "SELECT real_path FROM project")]
    for name in ("完整規劃", "問題規劃", "套版整合"):
        assert str(work / name) in paths, f"{name} 被合併掉了"


def test_run_讀游標前就持有寫鎖(indexed, monkeypatch):
    """兩個 run() 同時跑時，後到的必須等前者 commit 才讀 scan_state，
    否則 turn / file_touch（無 UNIQUE）會插成兩份。守法：每段管線
    進入時已經在 BEGIN IMMEDIATE 的交易裡。"""
    seen = []
    real = indexer.index_transcript

    def spy(con, resolver, path):
        seen.append(con.in_transaction)
        return real(con, resolver, path)

    monkeypatch.setattr(indexer, "index_transcript", spy)
    indexer.run(full=False)
    assert seen and all(seen)


def test_watch_跑到_max_runs_就停(indexed):
    assert indexer.watch(interval=0, max_runs=2) == 2


def test_watch_拒絕_full():
    """--full 會刪掉 DB 檔，讓正在跑的 server 抱著失效的 fd。"""
    with pytest.raises(SystemExit) as caught:
        indexer.main(["--watch", "--full"])
    assert caught.value.code == 2


def test_busy_timeout_已設定(indexed):
    """watcher 與 server 同時寫時，慢的一方要等，不是直接報錯。"""
    import server

    for con in (indexer.connect(), server.db()):
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        con.close()


def test_watch_遇到鎖不會退出(indexed, monkeypatch, capsys):
    real_run, calls = indexer.run, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(indexer, "run", flaky)
    assert indexer.watch(interval=0, max_runs=2) == 2
    assert "DB 忙" in capsys.readouterr().out


# ── progress_signal：session_id IS NULL 的去重 ────────────────────────────

def memory_rows(db):
    return rows(db, "SELECT id, origin, ts, body FROM progress_signal "
                    "WHERE kind = 'memory_file' ORDER BY id")


def test_memory_file_不會因為_session_id_NULL_重複(indexed):
    """UNIQUE(session_id, …) 對 NULL 無效 —— 每跑一次索引就多一份，時間軸會洗版。"""
    db = indexed["db"]
    before = memory_rows(db)
    assert len(before) == 1, "合成樹裡只有一個 brain.md，測試不能空轉"

    for _ in range(3):
        indexer.run(full=False)

    after = memory_rows(db)
    assert len(after) == 1, f"增量三次後變成 {len(after)} 份"
    assert [r["id"] for r in after] == [r["id"] for r in before], "原本那列被換掉了"


def test_migrate_會清掉既有的_memory_重複列(indexed):
    db = indexed["db"]
    src = memory_rows(db)[0]

    con = sqlite3.connect(db)
    for _ in range(4):                      # 模擬修好之前累積的 5 份
        con.execute(
            "INSERT INTO progress_signal"
            "(session_id, project_id, ts, kind, goal, next_step, body, origin) "
            "SELECT NULL, project_id, ts, kind, goal, next_step, body, origin "
            "FROM progress_signal WHERE id = ?", (src["id"],))
    con.commit()
    con.close()
    assert len(memory_rows(db)) == 5

    indexer.connect().close()               # connect() 會跑 migrate()
    left = memory_rows(db)
    assert [r["id"] for r in left] == [src["id"]], "沒有只留下最早的那一列"

    indexer.connect().close()               # 冪等：再跑一次不該再動任何東西
    assert memory_rows(db) == left
