"""HTTP API —— 用 TestClient 打，資料來自合成的 ~/.claude。"""


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
