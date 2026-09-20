"""每週回顧 /api/week。

conftest 的合成資料剛好跨週：prompt 在 2026-08-29（週六），
commit 與改檔在 2026-09-01（週二）—— 所以「這週有 commit 卻沒有 prompt」
這個容易寫錯的情況本來就在測試裡，專案清單不能只看 prompt。
"""

import datetime as dt

MONDAY = "2026-08-31"          # commit 那週的週一
KEYS = {"start", "end", "projects", "commits", "flags", "tokens", "prev"}


def test_week_預設回本週一(client):
    d = client.get("/api/week").json()
    assert set(d) == KEYS
    start = dt.date.fromisoformat(d["start"])
    assert start.weekday() == 0, f"{d['start']} 不是週一"
    assert dt.date.fromisoformat(d["end"]) - start == dt.timedelta(days=7)


def test_week_指定_start_含_prompt_與_commit(client):
    d = client.get("/api/week", params={"start": MONDAY}).json()
    assert d["start"] == MONDAY and d["end"] == "2026-09-07"
    assert d["projects"], "這週有 commit 與改檔，專案清單不該是空的"

    names = [c["message"] for c in d["commits"]]
    assert "feat: 加上設定" in names

    alpha = next(p for p in d["projects"] if p["display_name"] == "alpha")
    assert alpha["commits"] == 1 and alpha["files"] == 1
    assert alpha["sessions"] == 1, "只有 commit / 改檔的 session 被漏掉了"
    assert alpha["goal"] == "ship the config change."
    assert alpha["next_step"] == "run the tests."

    counts = [p["prompts"] for p in d["projects"]]
    assert counts == sorted(counts, reverse=True), "projects 沒有依 prompts 由多到少"

    # 上一週才有 prompt，這週的 prompt 數必須是 0（ts 比較是 >= start AND < end）
    assert sum(counts) == 0


def test_week_空週回零(client):
    r = client.get("/api/week", params={"start": "2020-01-06"})
    assert r.status_code == 200
    d = r.json()
    assert d["projects"] == [] and d["commits"] == [] and d["flags"] == []
    assert d["tokens"] == {"calls": 0, "output": 0, "input_all": 0, "cache_read": 0}
    assert d["prev"] == {"prompts": 0, "commits": 0, "tokens_out": 0}


def test_week_prev_對照(client):
    d = client.get("/api/week", params={"start": MONDAY}).json()
    assert set(d["prev"]) == {"prompts", "tokens_out", "commits"}
    # 08-29 的兩則真人 prompt 落在上一週（/effort 是 slash，不算）
    assert d["prev"]["prompts"] == 2
    assert d["prev"]["commits"] == 0

    last = client.get("/api/week", params={"start": "2026-08-24"}).json()
    assert sum(p["prompts"] for p in last["projects"]) == d["prev"]["prompts"]


def test_week_start_壞掉回_400(client):
    assert client.get("/api/week", params={"start": "上週"}).status_code == 400
    assert client.get("/api/week", params={"start": "2026-02-30"}).status_code == 400
