"""Gemini CLI 解析。

fixture 依 docs/gemini-schema.md 的實測樣本自己造，守住那份文件點名的三個陷阱：
  1. 第一則 type=="user" 是 CLI 注入的 <session_context>，不是真人。
  2. `$set.messages` 會整批重設訊息表 -> 檔案變動必須整份重播 + 先刪再插。
  3. `.project_root` 全小寫 -> 不能直接建 project，會多一張大小寫重複的卡。

`type:"gemini"` 的訊息與 `toolCalls` 在勘查時 **NOT OBSERVED**（本機沒有認證），
所以那部分的 fixture 是照源碼欄位造的，測的是「防禦性解析不會爆、欄位名多認幾個」。
"""

import json
import sqlite3

import gemini
import indexer
import pytest


def write_session(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def q(db, sql, *args):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return out


SESSION_ID = "63a95ada-876f-483a-bc98-099900d7ee46"

HEADER = {"sessionId": SESSION_ID, "projectHash": "7e7e4c2254646919",
          "startTime": "2026-09-20T10:18:01.708Z",
          "lastUpdated": "2026-09-20T10:18:01.708Z", "kind": "main"}

# CLI 注入的環境 context —— 整棵目錄樹，不是真人打的
CONTEXT = {"$set": {"messages": [{
    "id": gemini.SESSION_CONTEXT_ID,
    "timestamp": "2026-09-20T10:18:01.710Z", "type": "user",
    "content": [{"text": "<session_context>\nThis is the Gemini CLI.\n"
                         "- **Directory Structure:**\n└───before.txt\n</session_context>"}],
}], "lastUpdated": "2026-09-20T10:18:01.710Z"}}

HUMAN = {"id": "495eefe3-93e3-4359-bf5d-c25557c57f81",
         "timestamp": "2026-09-20T10:18:01.875Z", "type": "user",
         "content": [{"text": "把 auth.ts 的登入 bug 修掉"}]}

# 【源碼】欄位，本機沒觀察到；content 故意用純字串（recordMessage 的寫法）
REPLY = {"id": "a1", "timestamp": "2026-09-20T10:18:09.000Z", "type": "gemini",
         "content": "改好了，改用非空判斷。",
         "model": "gemini-2.5-pro",
         "thoughts": [{"subject": "先看檔案", "description": "讀 auth.ts",
                       "timestamp": "2026-09-20T10:18:05.000Z"}],
         "tokens": {"input": 5000, "output": 200, "total": 5200},
         "toolCalls": [
             {"id": "c1", "name": "write_file", "status": "success",
              "args": {"file_path": "auth.ts", "content": "…"},
              "timestamp": "2026-09-20T10:18:06.000Z"},
             {"id": "c2", "name": "run_shell_command", "status": "success",
              "args": {"command": "pytest -q"},
              "timestamp": "2026-09-20T10:18:07.000Z"}]}

TOUCH = {"$set": {"lastUpdated": "2026-09-20T10:18:09.000Z"}}

ROWS = [HEADER, CONTEXT, HUMAN, TOUCH, REPLY, {"$set": {"sessionId": SESSION_ID}}]


# ── 最關鍵的判別式 ────────────────────────────────────────────────────────

def test_注入訊息靠固定_id_擋得掉():
    injected = CONTEXT["$set"]["messages"][0]
    assert gemini.is_real_user_prompt(injected) is False
    assert gemini.events(injected) == []


def test_注入訊息換了_id_靠文字前綴還是擋得掉():
    """id 是 0.44.1 的實作細節，換版本可能變；文字前綴才是保證。"""
    injected = dict(CONTEXT["$set"]["messages"][0], id="換了一個 id")
    assert gemini.is_real_user_prompt(injected) is False


def test_真人_prompt_過得了判別式():
    ev = gemini.events(HUMAN)
    assert [e["kind"] for e in ev] == ["user"]
    assert ev[0]["text"] == "把 auth.ts 的登入 bug 修掉"


def test_合成的空白_user_訊息不算():
    assert gemini.is_real_user_prompt(
        {"type": "user", "id": "x", "content": [{"text": "   "}]}) is False


# ── content 兩種型別 ──────────────────────────────────────────────────────

def test_content_純字串與_Part_陣列都吃得到():
    assert gemini.text_of("純字串") == "純字串"
    assert gemini.text_of([{"text": "a"}, {"text": "b"}]) == "ab"
    assert gemini.text_of([{"inlineData": {}}, "尾巴"]) == "尾巴"
    assert gemini.text_of({"不是預期的形狀": 1}) == ""
    assert gemini.text_of(None) == ""


# ── 重播 ──────────────────────────────────────────────────────────────────

def test_replay_set_messages_會整批重設():
    _, messages = gemini.replay([
        HEADER,
        {"id": "m1", "type": "user", "content": [{"text": "舊的"}]},
        {"$set": {"messages": [{"id": "m9", "type": "user",
                                "content": [{"text": "新的"}]}]}},
    ])
    assert [m["id"] for m in messages] == ["m9"]


def test_replay_同_id_會覆寫而不是多一則():
    _, messages = gemini.replay([
        {"id": "m1", "type": "gemini", "content": "執行中", "toolCalls": []},
        {"id": "m1", "type": "gemini", "content": "做完了",
         "toolCalls": [{"id": "c1", "name": "write_file",
                        "args": {"path": "a.py"}, "status": "success"}]},
    ])
    assert len(messages) == 1 and messages[0]["content"] == "做完了"


def test_replay_rewindTo_砍掉該則之後的全部():
    rows = [{"id": f"m{i}", "type": "user", "content": [{"text": str(i)}]}
            for i in range(4)]
    _, messages = gemini.replay(rows + [{"$rewindTo": "m2"}])
    assert [m["id"] for m in messages] == ["m0", "m1"]
    _, cleared = gemini.replay(rows + [{"$rewindTo": "不存在的 id"}])
    assert cleared == []


def test_replay_檔頭與_set_都併進_metadata():
    meta, _ = gemini.replay(ROWS)
    assert meta["sessionId"] == SESSION_ID
    assert meta["startTime"] == "2026-09-20T10:18:01.708Z"


def test_孤兒檔沒有真人也沒有回覆_不值得索引():
    """--resume 會另外開一個只有檔頭 + <session_context> 的檔案。"""
    _, messages = gemini.replay([HEADER, CONTEXT])
    assert gemini.worth_indexing(messages) is False
    assert gemini.worth_indexing(gemini.replay(ROWS)[1]) is True


def test_同_sessionId_的兩個檔不會撞在一起(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_HOME", tmp_path)
    chats = tmp_path / "tmp" / "slug" / "chats"
    chats.mkdir(parents=True)
    a = gemini.session_id_of(chats / "session-2026-09-20T10-18-63a95ada.jsonl", SESSION_ID)
    b = gemini.session_id_of(chats / "session-2026-09-20T10-19-63a95ada.jsonl", SESSION_ID)
    assert a != b and a.startswith(SESSION_ID) and b.startswith(SESSION_ID)


# ── 防禦性解析（形狀全部 NOT OBSERVED）──────────────────────────────────

@pytest.mark.parametrize("msg", [
    None, "不是 dict", 42,
    {"type": "gemini"},                                   # 什麼都沒有
    {"type": "gemini", "content": {"怪形狀": 1}, "toolCalls": "不是陣列",
     "thoughts": 5, "tokens": "也不是 dict"},
    {"type": "gemini", "toolCalls": ["字串", None, {}]},   # 元素不是 dict
    {"type": "info", "content": "合成訊息"},               # 不該進資料庫
])
def test_認不得的形狀不會爆(msg):
    got = gemini.events(msg)
    assert isinstance(got, list)
    assert all(e["kind"] != "user" for e in got)


def test_缺_toolCalls_的_gemini_訊息只出回覆():
    got = gemini.events({"type": "gemini", "timestamp": "t",
                         "content": [{"text": "只有文字"}]})
    assert [e["kind"] for e in got] == ["assistant"]


def test_檔案路徑欄位名三種別名都吃得到():
    for key in ("file_path", "path", "absolute_path"):
        assert gemini.file_target({key: "a.py"}) == "a.py"
    assert gemini.file_target({"沒有路徑": 1}) == ""
    assert gemini.file_target("不是 dict") == ""


def test_工具事件的順序讓錯誤算在同一回合():
    """tool_output 排在 assistant 之前 —— 同一則訊息裡的錯誤屬於這一回合。"""
    got = gemini.events(dict(REPLY, toolCalls=[
        {"id": "c1", "name": "write_file", "status": "error",
         "args": {"file_path": "a.py"}, "resultDisplay": "permission denied"}]))
    kinds = [e["kind"] for e in got]
    assert kinds.index("tool_output") < kinds.index("assistant")
    assert got[kinds.index("tool_output")]["has_error"] is True


# ── 端到端 ────────────────────────────────────────────────────────────────

@pytest.fixture
def gemini_home(tmp_path, monkeypatch, fake_home):
    home = fake_home["gemini"]
    slug = home / "tmp" / "geminiproj"
    (slug / "chats").mkdir(parents=True)
    work = fake_home["work"] / "geminiproj"
    work.mkdir()
    # 實測：.project_root 是**全小寫**、無結尾換行
    (slug / ".project_root").write_text(str(work).lower(), encoding="utf-8")

    chat = slug / "chats" / "session-2026-09-20T10-18-63a95ada.jsonl"
    write_session(chat, ROWS)
    # --resume 留下的孤兒檔：同一個 sessionId，但永遠沒有真人訊息
    write_session(slug / "chats" / "session-2026-09-20T10-19-63a95ada.jsonl",
                  [HEADER, CONTEXT])

    monkeypatch.setattr(gemini, "GEMINI_HOME", home)
    indexer.run(full=True)
    return {"db": fake_home["db"], "work": work, "chat": chat, "home": home}


def test_session_context_不算_prompt(gemini_home):
    texts = [r["text"] for r in
             q(gemini_home["db"], "SELECT text FROM prompt WHERE tool = 'gemini'")]
    assert texts == ["把 auth.ts 的登入 bug 修掉"]
    assert not any("session_context" in t or "Directory Structure" in t for t in texts)


def test_tool_事件抽出改檔(gemini_home):
    files = q(gemini_home["db"],
              "SELECT path, verb FROM file_touch WHERE path = 'auth.ts'")
    assert files and files[0]["verb"] == "Write"
    cmds = q(gemini_home["db"],
             "SELECT command, kind FROM command_run WHERE command = 'pytest -q'")
    assert cmds and cmds[0]["kind"] == "test"
    turn = q(gemini_home["db"], "SELECT assistant_summary, tools_json FROM turn "
                                "WHERE assistant_summary LIKE '改好了%'")
    assert turn and json.loads(turn[0]["tools_json"]) == ["write_file",
                                                          "run_shell_command"]


def test_project_root_小寫也對回同一專案(gemini_home):
    work = gemini_home["work"]
    rows = q(gemini_home["db"], "SELECT real_path FROM project "
                                "WHERE real_path = ? COLLATE NOCASE", str(work))
    assert len(rows) == 1, f"大小寫不同的重複卡: {rows}"
    got = q(gemini_home["db"],
            "SELECT p.real_path FROM session s JOIN project p ON p.id = s.project_id "
            "WHERE s.tool = 'gemini'")
    assert got and got[0]["real_path"].lower() == str(work).lower()


def test_資料夾不在了就用_NOCASE_對回既有列(gemini_home):
    con = indexer.connect()
    con.execute("INSERT INTO project(real_path, display_name) VALUES (?, ?)",
                (r"D:\Gone\MixedCase", "MixedCase"))
    con.commit()
    before = con.execute("SELECT COUNT(*) FROM project").fetchone()[0]
    pid = indexer.gemini_project(con, indexer.Resolver(con), r"d:\gone\mixedcase")
    row = con.execute("SELECT real_path FROM project WHERE id = ?", (pid,)).fetchone()
    after = con.execute("SELECT COUNT(*) FROM project").fetchone()[0]
    con.close()
    assert row[0] == r"D:\Gone\MixedCase" and after == before


def test_覆寫式檔案重讀不重複(gemini_home):
    db, chat = gemini_home["db"], gemini_home["chat"]

    def counts():
        return {t: q(db, f"SELECT COUNT(*) n FROM {t} WHERE session_id LIKE ?",
                     f"{SESSION_ID}%")[0]["n"]
                for t in ("prompt", "turn", "file_touch", "command_run")}

    first = counts()
    assert first["prompt"] == 1 and first["file_touch"] == 1

    # 沒有任何變動 -> 游標判定沒變，整份跳過，筆數不能動
    indexer.run()
    assert counts() == first

    # $set.messages 把訊息整批換成更短的一組 -> 舊的列必須消失，不能疊加
    write_session(chat, [HEADER, CONTEXT, {"$set": {"messages": [
        dict(HUMAN, content=[{"text": "第一題"}]),
        dict(HUMAN, id="u2", timestamp="2026-09-20T10:20:00.000Z",
             content=[{"text": "第二題"}]),
    ]}}])
    indexer.run()
    second = counts()
    assert second == {"prompt": 2, "turn": 0, "file_touch": 0, "command_run": 0}

    indexer.run()
    assert counts() == second, "第二次增量又插了一份"

    # 洗成只剩 session_context（沒有任何真人／gemini 訊息）-> 舊列與 session 列都要走
    write_session(chat, [HEADER, CONTEXT, {"$set": {"messages": []}}])
    indexer.run()
    assert counts() == {"prompt": 0, "turn": 0, "file_touch": 0, "command_run": 0}
    assert q(db, "SELECT COUNT(*) n FROM session WHERE id LIKE ?", f"{SESSION_ID}%")[0]["n"] == 0


def test_孤兒檔不會生出空的_session(gemini_home):
    rows = q(gemini_home["db"], "SELECT id FROM session WHERE tool = 'gemini'")
    assert len(rows) == 1, f"孤兒檔被索引了: {rows}"


def test_三種_CLI_並存不互相污染(gemini_home):
    rows = q(gemini_home["db"], "SELECT tool, COUNT(*) n FROM prompt GROUP BY tool")
    tools = {r["tool"]: r["n"] for r in rows}
    assert tools.get("claude", 0) > 0 and tools.get("gemini", 0) > 0


def test_沒裝_gemini_整段跳過(fake_home, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(gemini, "GEMINI_HOME", tmp_path / "沒有這個目錄")
    assert gemini.available() is False
    assert gemini.session_files() == []
    indexer.run(full=True)                       # 不該拋例外
    assert "Gemini" not in capsys.readouterr().out
    assert q(fake_home["db"], "SELECT * FROM prompt WHERE tool = 'gemini'") == []
