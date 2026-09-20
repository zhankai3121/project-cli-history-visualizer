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
