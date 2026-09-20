"""專案標記（釘選 / 標籤 / 筆記）—— 使用者手寫的資料，索引不能把它弄丟。"""

import indexer


def alpha_id(client):
    """conftest 的 alpha 專案。--full 之後 id 會重排，所以每次重新問。"""
    ps = client.get("/api/overview").json()["projects"]
    return next(p["id"] for p in ps if p["display_name"] == "alpha")


def test_meta_預設值(client):
    meta = client.get(f"/api/project/{alpha_id(client)}/meta").json()
    assert meta == {"pinned": 0, "tags": [], "note": None, "updated_at": None}


def test_meta_部分更新與_tags_清洗(client):
    pid = alpha_id(client)
    r = client.put(f"/api/project/{pid}/meta",
                   json={"tags": [" 後端 ", "後端", "", "  ", "前端"]})
    assert r.status_code == 200
    assert r.json()["tags"] == ["後端", "前端"]          # strip、去空、去重且保序

    # 只給 pinned，tags 與 note 要沿用舊值（合併而不是覆寫整列）
    merged = client.put(f"/api/project/{pid}/meta", json={"pinned": True}).json()
    assert merged["pinned"] == 1 and merged["tags"] == ["後端", "前端"]
    assert client.get(f"/api/project/{pid}/meta").json() == merged

    assert client.put(f"/api/project/{pid}/meta",
                      json={"tags": [f"t{i}" for i in range(21)]}).status_code == 400
    assert client.put(f"/api/project/{pid}/meta",
                      json={"tags": ["x" * 31]}).status_code == 400


def test_tags_出現在_overview_與_api_tags(client):
    ps = client.get("/api/overview").json()["projects"]
    a = next(p["id"] for p in ps if p["display_name"] == "alpha")
    b = next(p["id"] for p in ps if p["display_name"] == "beta")
    client.put(f"/api/project/{a}/meta",
               json={"pinned": True, "tags": ["共用", "只有 alpha"], "note": "第一行\n第二行"})
    client.put(f"/api/project/{b}/meta", json={"tags": ["共用"]})

    card = next(p for p in client.get("/api/overview").json()["projects"]
                if p["id"] == a)
    assert card["pinned"] == 1
    assert card["tags"] == ["共用", "只有 alpha"]        # list，不是 JSON 字串
    assert card["note"].startswith("第一行")

    tags = client.get("/api/tags").json()["tags"]
    assert tags[0] == {"tag": "共用", "n": 2}            # 次數多的排前面
    assert {"tag": "只有 alpha", "n": 1} in tags


def test_full_重建保留_meta(client):
    pid = alpha_id(client)
    before = client.put(f"/api/project/{pid}/meta",
                        json={"pinned": True, "tags": ["留著"], "note": "手寫的"}).json()
    indexer.run(full=True)                               # 砍掉整個 DB 重建
    assert client.get(f"/api/project/{alpha_id(client)}/meta").json() == before


def test_tags_欄位壞掉不會讓總覽_500(client):
    """手動改壞 DB 的 tags JSON：overview / meta / tags 都要照常回，當成沒標籤。"""
    import sqlite3

    import indexer

    pid = alpha_id(client)
    real_path = client.get(f"/api/project/{pid}").json()["project"]["real_path"]
    con = sqlite3.connect(indexer.DB_PATH)
    con.execute("INSERT OR REPLACE INTO project_meta(real_path, pinned, tags) VALUES (?, 1, ?)",
                (real_path, "{not json"))
    con.commit()
    con.close()
    assert client.get("/api/overview").status_code == 200
    assert client.get(f"/api/project/{pid}/meta").json()["tags"] == []
    assert client.get("/api/tags").json()["tags"] == []


def test_型別錯回_400(client):
    pid = alpha_id(client)
    for bad in ({"pinned": "yes"}, {"tags": "後端,前端"}, {"tags": [1]},
                {"note": 123}, {"note": "x" * 4001}):
        assert client.put(f"/api/project/{pid}/meta", json=bad).status_code == 400, bad
    assert client.get("/api/project/99999/meta").status_code == 404
    assert client.put("/api/project/99999/meta", json={"pinned": True}).status_code == 404
