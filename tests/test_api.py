"""HTTP API —— 用 TestClient 打，資料來自合成的 ~/.claude。"""

import json
import os

import indexer
import pytest
from conftest import assistant, jsonl


def test_首頁出得來(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "CLI History Visualizer" in r.text


def test_overview_預設不含容器(client):
    ps = client.get("/api/overview").json()["projects"]
    assert ps
    assert all(p["is_container"] == 0 for p in ps)


def test_overview_可以叫出容器(client):
    a = len(client.get("/api/overview").json()["projects"])
    b = len(client.get("/api/overview?include_containers=true").json()["projects"])
    assert b >= a


def test_only_history_會變少(client):
    allp = client.get("/api/overview").json()["projects"]
    hist = client.get("/api/overview?only_history=true").json()["projects"]
    assert len(hist) <= len(allp)
    assert all(p["has_history"] == 1 for p in hist)


def test_進度卡有目標與下一步(client):
    ps = client.get("/api/overview").json()["projects"]
    hit = [p for p in ps if p.get("goal")]
    assert hit and hit[0]["goal"] == "ship the config change."
    assert hit[0]["next_step"] == "run the tests."


def test_搜尋_prompt(client):
    d = client.get("/api/search", params={"q": "第一個問題"}).json()
    assert d["count"] >= 1
    assert any(h["kind"] == "prompt" for h in d["hits"])


def test_搜尋涵蓋_assistant_回覆(client):
    """turn_fts 之前完全沒被查詢過，這條防止再次失聯。"""
    d = client.get("/api/search", params={"q": "設定檔", "scope": "reply"}).json()
    assert d["replies"] >= 1
    assert all(h["kind"] == "reply" for h in d["hits"])


def test_兩字中文走_LIKE_fallback(client):
    """trigram 對 <3 字元是靜默回 0 筆，不是報錯 —— 所以一定要有 fallback。"""
    d = client.get("/api/search", params={"q": "部署"}).json()
    assert d["count"] >= 1
    assert "like" in d["mode"]


def test_查無結果不會報錯(client):
    d = client.get("/api/search", params={"q": "絕對不存在的字串zzz"}).json()
    assert d["count"] == 0


def test_scope_限定範圍(client):
    p = client.get("/api/search", params={"q": "第一個問題", "scope": "prompt"}).json()
    assert all(h["kind"] == "prompt" for h in p["hits"])


def test_專案詳情與_session(client):
    pid = client.get("/api/overview").json()["projects"][0]["id"]
    d = client.get(f"/api/project/{pid}").json()
    assert "sessions" in d
    if d["sessions"]:
        sid = d["sessions"][0]["id"]
        s = client.get(f"/api/session/{sid}").json()
        assert "prompts" in s and "turns" in s


def test_不存在的專案回_404(client):
    assert client.get("/api/project/999999").status_code == 404


def test_熱度圖與最近動態(client):
    assert client.get("/api/heatmap").json()["days"]
    assert "hits" in client.get("/api/recent").json()


def test_roots_讀寫(client, indexed):
    before = client.get("/api/roots").json()["roots"]
    assert before
    r = client.post("/api/roots", json={"roots": [str(indexed["work"])]})
    assert r.status_code == 200


def test_roots_擋掉不存在的路徑(client):
    r = client.post("/api/roots", json={"roots": ["Z:\\不存在的資料夾"]})
    assert r.status_code == 400


def test_browse_列得出子目錄(client, indexed):
    d = client.get("/api/browse", params={"path": str(indexed["work"])}).json()
    names = [e["name"] for e in d["dirs"]]
    assert "alpha" in names and "beta" in names
    assert all(e["path"].endswith(e["name"]) for e in d["dirs"])


def test_browse_不存在的路徑回_404(client):
    assert client.get("/api/browse", params={"path": "Z:\\無"}).status_code == 404


# ── F8 搜尋強化 ───────────────────────────────────────────────────────────
# 合成資料的時間軸：prompt 都在 2026-08-29，assistant 摘要在 2026-09-01。


def test_搜尋_日期範圍(client):
    def hit(**extra):
        return client.get("/api/search", params={"q": "設定檔", **extra}).json()

    assert hit(since="2026-09-02")["count"] == 0
    got = hit(since="2026-09-01")
    assert got["count"] >= 1 and got["since"] == "2026-09-01"
    assert hit(until="2026-09-01")["count"] >= 1, "until 應該含當天"
    assert hit(until="2026-08-31")["count"] == 0
    # prompt 那條路徑也要吃到範圍（prompt 全在 08-29）
    assert client.get("/api/search",
                      params={"q": "第一個問題", "since": "2026-09-01"}).json()["count"] == 0


def test_slash_only_只回_slash_prompt(client):
    d = client.get("/api/search", params={"q": "effort", "slash": "only"}).json()
    assert d["count"] >= 1 and d["slash"] == "only"
    assert all(h["kind"] == "prompt" and h["is_slash"] == 1 for h in d["hits"])
    assert d["replies"] == 0 and d["agents"] == 0
    assert client.get("/api/search",
                      params={"q": "第一個問題", "slash": "only"}).json()["count"] == 0


def test_slash_exclude(client):
    assert client.get("/api/search",
                      params={"q": "effort", "slash": "exclude"}).json()["count"] == 0
    d = client.get("/api/search", params={"q": "第一個問題", "slash": "exclude"}).json()
    assert d["count"] >= 1
    assert all(h["is_slash"] == 0 for h in d["hits"])


def test_FTS_snippet_有_mark(client):
    d = client.get("/api/search", params={"q": "第一個問題"}).json()
    assert "fts" in d["mode"] and d["hits"]
    assert all("<mark>" in h["snippet"] for h in d["hits"])


def test_LIKE_fallback_也有_snippet(client):
    """<3 字走 LIKE，沒有 snippet() 可用，視窗與 mark 都是 Python 做的。"""
    d = client.get("/api/search", params={"q": "部署"}).json()
    assert "like" in d["mode"] and d["hits"]
    assert "<mark>部署</mark>" in d["hits"][0]["snippet"]


def test_snippet_已跳脫_HTML(fake_home):
    """snippet 會直接進 innerHTML —— prompt 裡的標籤必須先變成實體。"""
    from fastapi.testclient import TestClient

    import indexer
    import server
    from conftest import jsonl

    jsonl(fake_home["claude"] / "history.jsonl", [
        {"display": "標籤測試 <b>x</b> 結束", "timestamp": 1788000180000,
         "project": str(fake_home["work"] / "alpha"), "sessionId": "sess-1"},
    ])
    indexer.run(full=True)
    d = TestClient(server.app).get("/api/search", params={"q": "標籤測試"}).json()
    snips = [h["snippet"] for h in d["hits"]]
    assert snips, "合成的 prompt 沒被搜到"
    assert any("&lt;b&gt;" in s for s in snips)
    assert all("<b>" not in s for s in snips)
    assert any("<mark>" in s for s in snips), "跳脫之後 mark 還是要保留原樣"


def test_日期形狀對但不存在_當沒給(client):
    """2026-13-45 過得了 regex，SQLite date() 回 NULL 會靜默 0 筆。"""
    d = client.get("/api/search", params={"q": "設定檔", "since": "2026-13-45"}).json()
    assert d["since"] == "" and d["hits"]


def test_LIKE_mark_不會包進實體裡():
    """q=lt 時 '&lt;' 這種實體內部不能被 mark，否則畫面出現字面的 &lt;。"""
    import server

    out = server.mark_like("count < 5 and lt", "lt")
    assert out == "count &lt; 5 and <mark>lt</mark>"


# ── 檔案檢視（F2）────────────────────────────────────────────────────────

def append_jsonl(path, records):
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def edits(uuid, ts, *targets):
    """一筆 assistant，帶著幾個改檔工具。targets 是 (動詞, 絕對路徑)。"""
    rec = assistant("改一下。", tools=[(verb, {"file_path": str(p)})
                                       for verb, p in targets], ts=ts)
    rec["uuid"] = uuid
    return rec


@pytest.fixture
def with_files(fake_home):
    """alpha 專案：config.py 改 4 次（跨兩個 session）、app.py 2 次、src/main.py 1 次。"""
    alpha = fake_home["work"] / "alpha"
    config, app_py, main_py = (alpha / "config.py", alpha / "app.py",
                               alpha / "src" / "main.py")

    append_jsonl(fake_home["projects"] / "slug-alpha" / "sess-1.jsonl", [
        edits("a2", "2026-09-01T10:10:00.000Z", ("Edit", config)),
        edits("a3", "2026-09-01T10:20:00.000Z", ("Edit", config),
              ("Edit", app_py), ("Write", app_py)),
        edits("a4", "2026-09-01T10:30:00.000Z", ("Write", main_py)),
    ])
    # 第二個 session 也碰 config.py —— prompt 決定 started_at，所以兩邊都要寫
    append_jsonl(fake_home["claude"] / "history.jsonl", [
        {"display": "再調一次設定", "timestamp": 1788000180000,
         "project": str(alpha), "sessionId": "sess-3"},
    ])
    jsonl(fake_home["projects"] / "slug-alpha" / "sess-3.jsonl", [
        {"type": "session_start", "cwd": str(alpha), "sessionId": "sess-3",
         "timestamp": "2026-09-02T09:00:00.000Z"},
        edits("b1", "2026-09-02T09:05:00.000Z", ("Edit", config)),
    ])
    indexer.run(full=True)
    return fake_home


@pytest.fixture
def files_client(with_files):
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


def alpha_id(client):
    ps = client.get("/api/overview?include_containers=true").json()["projects"]
    return next(p["id"] for p in ps if p["real_path"].endswith("alpha"))


def test_專案檔案清單_依次數排序(files_client):
    pid = alpha_id(files_client)
    d = files_client.get(f"/api/project/{pid}/files").json()
    assert [f["rel"] for f in d["files"]][:2] == ["config.py", "app.py"]

    top = d["files"][0]
    assert top["n"] == 4 and top["edits"] == 4 and top["writes"] == 0
    assert top["sessions"] == 2, "跨 session 的改動沒被算進 sessions"
    assert top["agent_n"] == 0
    assert top["last_ts"] == "2026-09-02T09:05:00.000Z"

    app_py = d["files"][1]
    assert app_py["edits"] == 1 and app_py["writes"] == 1

    assert files_client.get("/api/project/999999/files").status_code == 404


def test_檔案_rel_去掉專案前綴(files_client):
    import server

    pid = alpha_id(files_client)
    d = files_client.get(f"/api/project/{pid}/files").json()
    root = d["root"]
    assert all(root.lower() not in f["rel"].lower() for f in d["files"]), \
        "rel 還帶著專案前綴，tag 上會是一長串絕對路徑"
    deep = next(f for f in d["files"] if f["rel"].endswith("main.py"))
    assert deep["rel"] in ("src/main.py", "src\\main.py")
    assert deep["path"].endswith(deep["rel"]) and len(deep["path"]) > len(deep["rel"])

    # 分隔符不同也要對得上；大小寫不同只有 Windows 上算同一個檔
    assert server.rel_to_project("C:\\work\\alpha\\a.py", "C:/work/alpha") == "a.py"
    if os.name == "nt":
        assert server.rel_to_project("C:\\Work\\Alpha\\a.py", "c:\\work\\alpha") == "a.py"
    # 去不掉就原樣回傳
    assert server.rel_to_project("D:\\其他\\a.py", "C:\\work\\alpha") == "D:\\其他\\a.py"


def test_檔案對應的_session(files_client):
    pid = alpha_id(files_client)
    target = files_client.get(f"/api/project/{pid}/files").json()["files"][0]["path"]
    d = files_client.get(f"/api/project/{pid}/file", params={"path": target}).json()

    assert d["path"] == target
    ids = [s["id"] for s in d["sessions"]]
    assert ids == ["sess-3", "sess-1"], "沒有依 started_at 由新到舊排"
    first = d["sessions"][0]
    assert first["n"] == 1 and first["verbs"] == "Edit" and first["via_agent_n"] == 0
    assert d["sessions"][1]["n"] == 3

    # 反斜線路徑要能原封不動走一趟 query 參數
    assert files_client.get(f"/api/project/{pid}/file",
                            params={"path": target.replace("/", "\\")}).status_code == 200


def test_檔案查無資料回空(files_client):
    pid = alpha_id(files_client)
    r = files_client.get(f"/api/project/{pid}/file",
                         params={"path": "C:\\沒有\\這個檔.py"})
    assert r.status_code == 200, "查無資料不該報錯"
    assert r.json()["sessions"] == []
    assert files_client.get("/api/project/999999/file",
                            params={"path": "x"}).status_code == 404


def test_timeline_依時間遞增且排除_last_prompt(client):
    """時間軸只收「會講目標」的訊號。last_prompt / cost_state 進來只會洗版。"""
    import sqlite3

    pid = alpha_id(client)
    con = sqlite3.connect(indexer.DB_PATH)
    con.execute("INSERT INTO progress_signal"
                "(session_id, project_id, ts, kind, goal, next_step, body, origin) "
                "VALUES ('sess-1', ?, '2026-09-01T11:00:00+00:00', 'last_prompt',"
                " NULL, NULL, '最後一句話', 'last-prompt')", (pid,))
    con.execute("INSERT INTO progress_signal"
                "(session_id, project_id, ts, kind, goal, next_step, body, origin) "
                "VALUES ('sess-1', ?, '2026-09-01T12:00:00+00:00', 'cost_state',"
                " NULL, NULL, '{}', 'cost-state')", (pid,))
    con.commit()
    con.close()

    items = client.get(f"/api/project/{pid}/timeline").json()["items"]
    assert len(items) == 2, "brain.md 與 away_summary 各一筆"
    assert {i["kind"] for i in items} == {"memory_file", "away_summary"}

    ts = [i["ts"] for i in items]
    assert ts == sorted(ts), "沒有依 ts 遞增"

    first, last = items
    assert first["origin"] == "brain.md" and first["session_id"] is None
    assert last["session_id"] == "sess-1" and last["title"] == "設定檔調整", \
        "沒有 LEFT JOIN session 取標題"
    assert last["goal"] == "ship the config change."
    assert set(first) == {"ts", "kind", "origin", "session_id", "title",
                          "goal", "state", "next_step"}

    assert client.get("/api/project/999999/timeline").status_code == 404


# ── F4 手寫 vs 自動 不一致 ────────────────────────────────────────────────
#
# 基準時間是「專案最後一筆 file_touch 的日期」，不是系統時鐘 —— 所以測試
# 要把專案的時鐘往前推（補一筆比較新的 touch），而不是去改 fixture 的日期。

def alpha_proj(client):
    ps = client.get("/api/overview?include_containers=true").json()["projects"]
    return next(p for p in ps if p["real_path"].endswith("alpha"))


def advance_clock(pid, real_path, ts="2026-10-25T09:00:00.000Z"):
    """替 alpha 補一筆新的 file_touch：專案往前走了，但 config.py 停在 09-01。"""
    import sqlite3

    con = sqlite3.connect(indexer.DB_PATH)
    con.execute("INSERT INTO file_touch(session_id, project_id, ts, path, verb) "
                "VALUES ('sess-1', ?, ?, ?, 'Edit')",
                (pid, ts, real_path + "/app.py"))
    con.commit()
    con.close()


def test_mismatch_提到的檔案沒動過(client):
    """brain.md 還在講 config.py，實作卻 54 天沒碰它 —— 這就是要報的那一格。"""
    p = alpha_proj(client)
    advance_clock(p["id"], p["real_path"])

    flag = alpha_proj(client)["mismatch"]
    assert flag and flag["kind"] == "mentioned_untouched"
    assert any(f.replace("\\", "/").endswith("/config.py") for f in flag["files"])
    assert "config.py" in flag["detail"] and "brain.md" in flag["detail"]
    assert flag["memory_ts"].startswith("2026-08-25")

    detail = client.get(f"/api/project/{p['id']}").json()["project"]
    assert detail["mismatch"]["kind"] == "mentioned_untouched", "專案頁沒有同一個旗標"


def test_mismatch_不存在的路徑不算(client):
    """`~/.claude/plans/…` 不是這個專案的檔案，提到它不能算「寫了沒做」。"""
    import sqlite3

    p = alpha_proj(client)
    advance_clock(p["id"], p["real_path"])      # 時鐘照推，擋掉的只能是路徑規則
    con = sqlite3.connect(indexer.DB_PATH)
    con.execute("UPDATE progress_signal SET body = ? "
                "WHERE project_id = ? AND kind = 'memory_file'",
                ("## Focus\n照 `~/.claude/plans/大計畫.md` 與 docs/never.py 做\n"
                 "\n## Next (when resuming)\n繼續\n", p["id"]))
    con.commit()
    con.close()

    # 候選全被濾掉 -> 規則一不報；規則二要 ≥10 筆 touch，這裡只有 3 筆 -> 也不報
    assert alpha_proj(client)["mismatch"] is None


def test_mismatch_手寫落後實作(client):
    """另一條規則：進度檔整份過期。落後 14 天以上、期間又改了 ≥10 次檔才算。"""
    import sqlite3

    p = alpha_proj(client)
    con = sqlite3.connect(indexer.DB_PATH)
    # body 不提任何專案檔 -> 規則一不成立，才測得到規則二
    con.execute("UPDATE progress_signal SET body = ? "
                "WHERE project_id = ? AND kind = 'memory_file'",
                ("## Focus\n把架構想清楚\n\n## Next (when resuming)\n繼續\n", p["id"]))
    for i in range(10):
        con.execute("INSERT INTO file_touch(session_id, project_id, ts, path, verb) "
                    "VALUES ('sess-1', ?, ?, ?, 'Edit')",
                    (p["id"], f"2026-10-25T09:0{i}:00.000Z", p["real_path"] + "/app.py"))
    con.commit()
    con.close()

    flag = alpha_proj(client)["mismatch"]
    assert flag and flag["kind"] == "stale_memory"
    assert "61 天" in flag["detail"] and "11 次" in flag["detail"], flag["detail"]
    assert flag["files"] == [], "整份過期時沒有特定檔案可指"


def test_memory_比所有改檔都新_不算不符(client):
    """剛寫好的 brain.md 提到很久沒動的檔 —— 那是計畫，不是「寫了沒做」。"""
    import sqlite3

    p = alpha_proj(client)
    advance_clock(p["id"], p["real_path"])      # config.py 落後 54 天，規則一本來會報
    con = sqlite3.connect(indexer.DB_PATH)
    con.execute("UPDATE progress_signal SET ts = '2026-10-30T00:00:00+00:00' "
                "WHERE project_id = ? AND kind = 'memory_file'", (p["id"],))
    con.commit()
    con.close()
    assert alpha_proj(client)["mismatch"] is None


def test_memory_跟上就沒有旗標(client):
    """brain.md 講的就是最近在改的檔，時間也沒落後 —— 一個旗標都不該有。"""
    ps = client.get("/api/overview?include_containers=true").json()["projects"]
    assert all("mismatch" in p for p in ps), "overview 少了 mismatch 欄"
    assert all(p["mismatch"] is None for p in ps), \
        [p["mismatch"] for p in ps if p["mismatch"]]

    # 根本沒有 memory 檔的專案也是 null，不是「查不到就報」
    beta = next(p for p in ps if p["real_path"].endswith("beta"))
    assert beta["mismatch"] is None
