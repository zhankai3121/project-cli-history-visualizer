"""git 狀態的增量快取。

每次都跑 git status 太慢（WSL 上單一 repo 0.5–0.9 秒，16 個 repo 共 3.8 秒）。
用 .git 底下幾個檔案的 mtime 當指紋，沒變就跳過；純改工作區不會動到 .git，
所以還要搭配 TTL 與 --force-git。
"""

import subprocess
import sqlite3

import indexer
import pytest


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


@pytest.fixture
def repo(fake_home):
    """在假的專案根目錄底下做一個真的 git repo。"""
    path = fake_home["work"] / "alpha"
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "a.txt").write_text("one", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    indexer.run(full=True)
    return {"path": path, "db": fake_home["db"]}


def project_row(db, path):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM project WHERE real_path = ?", (str(path),)).fetchone()
    con.close()
    return dict(row) if row else None


def count(con_path, force=False):
    con = sqlite3.connect(con_path)
    con.row_factory = sqlite3.Row
    got = indexer.collect_git(con, force=force)
    con.commit()
    con.close()
    return got


def test_第一次會讀到_git_狀態(repo):
    row = project_row(repo["db"], repo["path"])
    assert row["is_git"] == 1
    assert row["git_commits"] == 1
    assert row["git_dirty"] == 0
    assert row["git_last_msg"] == "first"
    assert row["git_signature"] and row["git_checked_at"]


def test_沒變動就跳過(repo):
    updated, skipped = count(repo["db"])
    assert updated == 0 and skipped >= 1


def test_force_會忽略快取(repo):
    updated, skipped = count(repo["db"], force=True)
    assert updated >= 1 and skipped == 0


def test_commit_之後指紋會變(repo):
    before = project_row(repo["db"], repo["path"])["git_signature"]
    (repo["path"] / "b.txt").write_text("two", encoding="utf-8")
    git(repo["path"], "add", "-A")
    git(repo["path"], "commit", "-q", "-m", "second")
    assert indexer.git_signature(repo["path"]) != before

    updated, _ = count(repo["db"])
    assert updated >= 1
    row = project_row(repo["db"], repo["path"])
    assert row["git_commits"] == 2 and row["git_last_msg"] == "second"


def test_指紋在跑完_git_之後才取(repo):
    """git status 自己可能重寫 .git/index。先取指紋的話快取會每次失效。"""
    count(repo["db"], force=True)
    stored = project_row(repo["db"], repo["path"])["git_signature"]
    assert stored == indexer.git_signature(repo["path"]), \
        "存的指紋與跑完之後的實際狀態不一致，下次比對一定不同"
    updated, skipped = count(repo["db"])
    assert updated == 0 and skipped >= 1


def test_ttl_過期會重讀(repo, monkeypatch):
    monkeypatch.setattr(indexer, "GIT_TTL_SECONDS", -1)
    updated, skipped = count(repo["db"])
    assert updated >= 1 and skipped == 0


def test_分支與未提交數一次取得(repo):
    (repo["path"] / "c.txt").write_text("three", encoding="utf-8")
    branch, dirty = indexer._status_v2(str(repo["path"]))
    assert branch in ("main", "master")
    assert dirty == 1


def test_不是_git_的目錄回_None(fake_home):
    assert indexer.git_signature(fake_home["work"] / "beta") is None


def test_讀不到_git_不會把狀態報成沒跟上(repo):
    """git 指令失敗時 traffic_light 必須回 None（無從判斷），不是 grey。"""
    import server
    row = project_row(repo["db"], repo["path"])
    row["git_last_ts"] = None
    assert server.traffic_light(row) is None
