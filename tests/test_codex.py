"""Codex CLI 解析。

fixture 依實測到的格式自己造，涵蓋最關鍵的那個陷阱：
真人 prompt 在 event_msg/user_message，不是 response_item 裡 role=="user"
的那些（那些是注入的 AGENTS.md 與環境資訊）。
"""

import json

import codex
import indexer
import pytest


def line(type_, payload, ts="2026-02-03T08:02:31.655Z"):
    return {"timestamp": ts, "type": type_, "payload": payload}


META = line("session_meta", {
    "id": "019c2286-484a-7550-b53b-cd4e1fd7c5e4",
    "timestamp": "2026-02-03T08:02:31.627Z",
    "cwd": "/srv/apps/billing", "originator": "codex_cli_rs",
    "cli_version": "0.92.0", "source": "cli", "model_provider": "openai",
    "git": {"branch": "main", "commit": "abc1234"}})

# 以下三行都是 role=="user" 的 response_item —— 全部都不是真人打的
INJECTED = [
    line("response_item", {"type": "message", "role": "user", "content": [
        {"type": "input_text",
         "text": "# AGENTS.md instructions for /srv/apps/billing\n你是資深工程師"}]}),
    line("response_item", {"type": "message", "role": "user", "content": [
        {"type": "input_text",
         "text": "<environment_context>\n  <cwd>/srv/apps/billing</cwd>\n</environment_context>"}]}),
    line("response_item", {"type": "message", "role": "developer", "content": [
        {"type": "input_text", "text": "<permissions instructions>…</permissions instructions>"}]}),
]

REAL_PROMPT = line("event_msg", {"type": "user_message",
                                 "message": "修掉 auth.ts 的登入 bug"})
REPLY = line("event_msg", {"type": "agent_message", "message": "改好了，改用非空判斷。"})


# ── 最關鍵的判別式 ────────────────────────────────────────────────────────

def test_真人_prompt_來自_event_msg():
    ev = codex.event(REAL_PROMPT)
    assert ev["kind"] == "user"
    assert ev["text"] == "修掉 auth.ts 的登入 bug"


@pytest.mark.parametrize("rec", INJECTED)
def test_注入的_role_user_不算真人(rec):
    """這條是整個 adapter 最重要的防線 —— 用 role 判斷會把 AGENTS.md 全文
    當成使用者 prompt 灌進資料庫。"""
    ev = codex.event(rec)
    assert ev is None or ev["kind"] != "user", f"把注入內容當成真人輸入了: {ev}"


def test_agent_message_是回覆():
    ev = codex.event(REPLY)
    assert ev["kind"] == "assistant" and "非空判斷" in ev["text"]


# ── session_meta ──────────────────────────────────────────────────────────

def test_session_meta_帶出專案路徑與_git():
    ev = codex.event(META)
    assert ev["kind"] == "meta"
    assert ev["cwd"] == "/srv/apps/billing"
    assert ev["git_branch"] == "main" and ev["git_commit"] == "abc1234"
    assert ev["originator"] == "codex_cli_rs"


# ── 工具 ──────────────────────────────────────────────────────────────────

def test_exec_command_抽出指令():
    ev = codex.event(line("response_item", {
        "type": "function_call", "name": "exec_command", "call_id": "c1",
        "arguments": json.dumps({"cmd": "cat auth.ts", "workdir": "/x"})}))
    assert ev["kind"] == "tool" and codex.is_shell_tool(ev["name"])
    assert codex.shell_command(ev["args"]) == "cat auth.ts"


def test_command_是陣列也吃得到():
    assert codex.shell_command({"command": ["git", "status", "--short"]}) \
        == "git status --short"


def test_arguments_不是合法_json_也不會爆():
    ev = codex.event(line("response_item", {
        "type": "function_call", "name": "shell", "arguments": "ls -la"}))
    assert codex.shell_command(ev["args"]) == "ls -la"


def test_apply_patch_抽出改過的檔案():
    ev = codex.event(line("response_item", {
        "type": "custom_tool_call", "status": "completed", "name": "apply_patch",
        "input": "*** Begin Patch\n*** Update File: /srv/apps/billing/a.ts\n@@\n-x\n+y\n"
                 "*** Add File: /srv/apps/billing/b.ts\n+new\n"
                 "*** Delete File: /srv/apps/billing/c.ts\n*** End Patch"}))
    assert ev["kind"] == "tool" and codex.is_file_tool(ev["name"])
    assert codex.patch_files(ev["args"]) == [
        ("/srv/apps/billing/a.ts", "Edit"), ("/srv/apps/billing/b.ts", "Write"),
        ("/srv/apps/billing/c.ts", "Delete")]


def test_指令失敗會被標記():
    ok = codex.event(line("response_item", {
        "type": "function_call_output", "call_id": "c1",
        "output": "Wall time: 0.05 seconds\nProcess exited with code 0\nOutput:\nhi"}))
    bad = codex.event(line("response_item", {
        "type": "function_call_output", "call_id": "c2",
        "output": "Process exited with code 1\nOutput:\nboom"}))
    assert ok["has_error"] is False
    assert bad["has_error"] is True


def test_reasoning_摘要():
    ev = codex.event(line("response_item", {
        "type": "reasoning", "encrypted_content": "…",
        "summary": [{"type": "summary_text", "text": "先看 auth.ts"}]}))
    assert ev["kind"] == "reasoning" and ev["text"] == "先看 auth.ts"


def test_token_count():
    ev = codex.event(line("event_msg", {
        "type": "token_count",
        "info": {"total_token_usage": {"input_tokens": 5000, "output_tokens": 200}}}))
    assert ev["kind"] == "tokens" and ev["input"] == 5000


def test_認不得的東西回_None():
    assert codex.event({"type": "whatever", "payload": {"type": "nope"}}) is None
    assert codex.event("不是 dict") is None
    assert codex.event({"type": "event_msg"}) is None       # 沒有 payload


# ── 檔名與時間 ────────────────────────────────────────────────────────────

def test_從檔名取_session_id(tmp_path):
    p = tmp_path / "rollout-2026-02-03T08-02-31-019c2286-484a-7550-b53b-cd4e1fd7c5e4.jsonl"
    assert codex.session_id_of(p) == "019c2286-484a-7550-b53b-cd4e1fd7c5e4"


def test_時間格式三種都吃():
    assert codex.iso("2026-02-03T08:02:31.655Z") == "2026-02-03T08:02:31.655Z"
    assert codex.iso(1770105751).startswith("2026-")        # epoch 秒
    assert codex.iso(1770105751000).startswith("2026-")     # epoch 毫秒
    assert codex.iso(None) is None


# ── history.jsonl ─────────────────────────────────────────────────────────

def test_history_entry():
    got = list(codex.history_rows([
        {"session_id": "s1", "ts": 1770105751, "text": "第一個問題"},
        {"session_id": "s1", "text": "缺 ts，跳過"},
        {"ts": 1770105752, "text": "缺 session_id，跳過"},
    ]))
    assert len(got) == 1 and got[0]["text"] == "第一個問題"


# ── 端到端 ────────────────────────────────────────────────────────────────

@pytest.fixture
def codex_home(tmp_path, monkeypatch, fake_home):
    home = tmp_path / ".codex"
    day = home / "sessions" / "2026" / "02" / "03"
    day.mkdir(parents=True)
    roll = day / ("rollout-2026-02-03T08-02-31-"
                  "019c2286-484a-7550-b53b-cd4e1fd7c5e4.jsonl")
    (fake_home["work"] / "codexproj").mkdir()
    meta = json.loads(json.dumps(META))
    meta["payload"]["cwd"] = str(fake_home["work"] / "codexproj")

    rows = [meta, *INJECTED, REAL_PROMPT,
            line("response_item", {"type": "function_call", "name": "exec_command",
                                   "arguments": json.dumps({"cmd": "pytest -q"})}),
            line("response_item", {"type": "function_call_output",
                                   "output": "Process exited with code 0"}),
            line("response_item", {"type": "custom_tool_call", "name": "apply_patch",
                                   "input": "*** Begin Patch\n*** Update File: auth.ts\n"
                                            "*** End Patch"}),
            REPLY]
    with open(roll, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    with open(home / "history.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"session_id": "019c2286-484a-7550-b53b-cd4e1fd7c5e4",
                             "ts": 1770105751, "text": "修掉 auth.ts 的登入 bug"}) + "\n")
        fh.write(json.dumps({"session_id": "已被刪掉的 session",
                             "ts": 1770105800, "text": "只剩 history 的那則"}) + "\n")

    monkeypatch.setattr(codex, "CODEX_HOME", home)
    indexer.run(full=True)
    return {"db": fake_home["db"], "work": fake_home["work"]}


def q(db, sql, *args):
    import sqlite3
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return out


def test_端到端_只收真人_prompt(codex_home):
    texts = [r["text"] for r in q(codex_home["db"],
                                  "SELECT text FROM prompt WHERE tool = 'codex'")]
    assert "修掉 auth.ts 的登入 bug" in texts
    assert not any("AGENTS.md" in t or "environment_context" in t for t in texts), \
        "注入內容被當成 prompt 收進來了"


def test_端到端_專案歸屬來自_session_meta(codex_home):
    row = q(codex_home["db"],
            "SELECT p.real_path FROM session s JOIN project p ON p.id = s.project_id "
            "WHERE s.tool = 'codex' AND s.transcript_state = 'live'")
    assert row and row[0]["real_path"] == str(codex_home["work"] / "codexproj")


def test_端到端_改檔與指令(codex_home):
    files = q(codex_home["db"], "SELECT path, verb FROM file_touch WHERE path = 'auth.ts'")
    assert files and files[0]["verb"] == "Edit"
    cmds = q(codex_home["db"], "SELECT command, kind FROM command_run "
                               "WHERE command = 'pytest -q'")
    assert cmds and cmds[0]["kind"] == "test"


def test_端到端_rollout_不在了也留得住_prompt(codex_home):
    got = q(codex_home["db"],
            "SELECT text FROM prompt WHERE session_id = '已被刪掉的 session'")
    assert got and got[0]["text"] == "只剩 history 的那則"


def test_兩種_CLI_並存不互相污染(codex_home):
    rows = q(codex_home["db"], "SELECT tool, COUNT(*) n FROM prompt GROUP BY tool")
    tools = {r["tool"]: r["n"] for r in rows}
    assert tools.get("claude", 0) > 0 and tools.get("codex", 0) > 0


def test_沒裝_codex_就整段跳過(fake_home, monkeypatch, tmp_path):
    monkeypatch.setattr(codex, "CODEX_HOME", tmp_path / "nope")
    assert codex.available() is False
    indexer.run(full=True)          # 不該拋例外
    assert q(fake_home["db"], "SELECT * FROM prompt WHERE tool = 'codex'") == []
