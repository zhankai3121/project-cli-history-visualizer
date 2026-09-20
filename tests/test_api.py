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
