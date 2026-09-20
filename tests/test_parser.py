"""parser.py 的判別式 —— 整個專案最容易靜默壞掉的地方。

每一條都對應 docs/jsonl-schema.md 裡實測出來的事實。改判別式之前先看那份。
"""

import indexer
import parser as P


# ── 系統目錄判別 ──────────────────────────────────────────────────────────

def test_windows_系統目錄():
    for p in [r"C:\Windows", r"C:\WINDOWS\system32", r"c:\windows\System32",
              r"C:\Program Files\Git", r"C:\Program Files (x86)\foo",
              r"C:\ProgramData\npm"]:
        assert indexer.is_system_path(p), p


def test_unix_系統目錄():
    for p in ["/usr/local/bin", "/etc/nginx", "/var/log", "/opt/tools", "/tmp/x"]:
        assert indexer.is_system_path(p), p


def test_wsl_裡的系統目錄():
    assert indexer.is_system_path(r"\\wsl.localhost\SomeDistro\usr\local")
    assert indexer.is_system_path(r"\\wsl$\SomeDistro\etc")


def test_不要誤殺使用者專案():
    """這組最重要 —— 誤判會讓真的專案從畫面上消失。"""
    for p in [r"C:\Users\someone\Desktop\code\myapp",
              r"C:\dev\windows-tools",          # 名字含 windows 但不是系統目錄
              r"C:\Users\x\optimizer",          # 含 opt
              r"C:\Users\x\Documents\etc-config",
              r"\\wsl.localhost\SomeDistro\home\someone\projects\app",
              r"\\wsl.localhost\SomeDistro\home\x\usr-manager",
              "/home/someone/projects/api",
              "/home/x/var-dump",
              "/mnt/c/Users/x/code"]:            # /mnt 刻意不排除
        assert not indexer.is_system_path(p), p


# ── 真人 prompt 判別 ──────────────────────────────────────────────────────

def human(**over):
    rec = {"type": "user", "origin": {"kind": "human"}, "promptSource": "typed",
           "message": {"role": "user", "content": "幫我改一下"}}
    rec.update(over)
    return rec


def test_真人輸入():
    assert P.is_real_user_prompt(human())


def test_queued_與_suggestion_也算真人():
    assert P.is_real_user_prompt(human(promptSource="queued"))
    assert P.is_real_user_prompt(human(promptSource="suggestion_accepted"))


def test_沒有_origin_不算():
    rec = human()
    del rec["origin"]
    assert not P.is_real_user_prompt(rec)


def test_task_notification_不算():
    assert not P.is_real_user_prompt(
        human(origin={"kind": "task-notification"}, promptSource="system"))


def test_sidechain_meta_compact_都不算():
    assert not P.is_real_user_prompt(human(isSidechain=True))
    assert not P.is_real_user_prompt(human(isMeta=True))
    assert not P.is_real_user_prompt(human(isCompactSummary=True))


def test_slash_command_展開不算():
    assert not P.is_real_user_prompt(human(message={
        "role": "user",
        "content": "<command-name>/model</command-name>\n<command-args></command-args>"}))


def test_tool_result_回填不算真人但要認得出來():
    rec = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}}
    assert not P.is_real_user_prompt(rec)
    assert P.is_tool_result_echo(rec)


def test_assistant_不是_user():
    assert not P.is_real_user_prompt({"type": "assistant", "message": {}})


# ── commit 訊息抽取 ───────────────────────────────────────────────────────

def first(cmd):
    got = P.extract_commit_messages(cmd)
    return got[0] if got else None


def test_heredoc_單引號():
    assert first("git commit -F- <<'EOF'\nfeat: a\n\nbody\nEOF") == "feat: a"


def test_heredoc_有空格的_dash():
    assert first("git commit -F - <<'MSG'\nfix: b\nMSG") == "fix: b"


def test_heredoc_雙引號():
    assert first('git commit -F- <<"EOF"\nrefactor: c\nEOF') == "refactor: c"


def test_heredoc_無引號():
    assert first("git commit -F- <<EOF\nchore: d\nEOF") == "chore: d"


def test_m_雙引號():
    assert first('git commit -m "docs: e"') == "docs: e"


def test_m_單引號():
    assert first("git commit -q -m 'test: f'") == "test: f"


def test_powershell_here_string():
    # 這個環境真實出現過，之前會抽出 "@'" 碎片
    assert first("git commit -m @'\ndocs: g\n\nbody\n'@") == "docs: g"
    assert first('git commit -m @"\nperf: h\n"@') == "perf: h"


def test_指令串接也吃得到():
    assert first("cd x && git add -A && git commit -q -F - <<'M'\nfeat: i\nM") == "feat: i"


def test_amend_no_edit_沒有訊息():
    assert P.extract_commit_messages("git commit --amend --no-edit") == []


def test_不是_commit_的指令不會誤抓():
    assert P.extract_commit_messages("git log -1 --format='%s'") == []
    assert P.extract_commit_messages("echo 'git commit is fun'") == []


def test_同一段指令多個_commit():
    got = P.extract_commit_messages(
        "git commit -m 'one' && git commit -m 'two'")
    assert got == ["one", "two"]


# ── away_summary ─────────────────────────────────────────────────────────

def test_英文_goal_next():
    goal, state, nxt = P.parse_away_summary(
        "Goal: ship it. Next: run the tests. (disable recaps in /config)")
    assert goal == "ship it."
    assert nxt == "run the tests."
    assert state is None


def test_中文散文用轉折詞切():
    goal, state, nxt = P.parse_away_summary(
        "手冊都寫完也驗過了。下一步是把設定接上去。")
    assert goal is None                      # 中文版沒有明示目標
    assert state == "手冊都寫完也驗過了"
    assert nxt == "把設定接上去。"


def test_中文其他轉折詞():
    for marker in ("接下來", "現在只差", "還差", "剩下唯一"):
        _, state, nxt = P.parse_away_summary(f"前半做完了。{marker}後半。")
        assert state == "前半做完了"
        assert nxt and marker in nxt


def test_沒有轉折詞就整段當狀態():
    _, state, nxt = P.parse_away_summary("就只是一句話。")
    assert state == "就只是一句話。"
    assert nxt is None


def test_空字串不會爆():
    assert P.parse_away_summary("") == (None, None, None)
    assert P.parse_away_summary(None) == (None, None, None)


# ── compact 摘要與 memory 檔 ─────────────────────────────────────────────

def test_compact_章節():
    goal, nxt = P.parse_compact_summary(
        "Summary:\n1. Primary Request and Intent:\n   做一個看板\n"
        "2. Pending Tasks:\n   接上搜尋\n")
    assert "做一個看板" in goal
    assert "接上搜尋" in nxt


def test_memory_markdown():
    goal, nxt = P.parse_memory_markdown(
        "# brain\n\n## Focus\n\n接 ASR\n\n## Next (when resuming)\n\n- 跑測試\n")
    assert "接 ASR" in goal
    assert "跑測試" in nxt


# ── 指令分類 ──────────────────────────────────────────────────────────────

def test_指令分類():
    assert P.classify_command("git status") == "git"
    assert P.classify_command("pytest -q") == "test"
    assert P.classify_command("npm run build") == "build"
    assert P.classify_command("pip install requests") == "install"
    assert P.classify_command("ls -la") == "other"


# ── JSONL 讀取 ────────────────────────────────────────────────────────────

def test_尾端半行不會推進游標(tmp_path):
    """session 正在寫入時尾端常是半行，不能因此吃掉資料。"""
    path = tmp_path / "t.jsonl"
    path.write_text('{"a":1}\n{"b":2}\n{"c":', encoding="utf-8")
    got = list(P.iter_jsonl(path))
    assert [rec for _, rec in got] == [{"a": 1}, {"b": 2}]
    last_offset = got[-1][0]
    # 從上次位置續讀，補完那半行之後應該讀得到
    path.write_text('{"a":1}\n{"b":2}\n{"c":3}\n', encoding="utf-8")
    assert [rec for _, rec in P.iter_jsonl(path, last_offset)] == [{"c": 3}]


def test_壞掉的行不會讓整個檔案掛掉(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"a":1}\n\n{"b":2}\n', encoding="utf-8")
    assert [rec for _, rec in P.iter_jsonl(path)] == [{"a": 1}, {"b": 2}]


# ── 手寫進度檔提到的路徑 ──────────────────────────────────────────────────

def test_memory_paths_抽反引號與副檔名():
    assert P.memory_paths("看 `server.py` 與 docs/plan.md，還有 foo") == \
        {"server.py", "docs/plan.md"}
    # 反斜線與大小寫都要正規化掉，不然結尾比對對不上 file_touch 裡的路徑
    assert P.memory_paths(r"改 `web\Index.HTML` 和 .\schema.sql") == \
        {"web/index.html", "schema.sql"}


def test_memory_paths_忽略沒有副檔名的字():
    """沒有副檔名的字一律不算 —— 不然中文進度檔會整篇變成候選路徑。"""
    assert P.memory_paths("重構 `parser` 模組，順便處理 indexer 這一塊") == set()
    assert P.memory_paths("") == set()
    # 指令裡的路徑要抽得出來，但整句指令本身不能變成候選
    assert P.memory_paths("`pytest tests/test_api.py -q`") == {"tests/test_api.py"}
    # x.py.bak 不能被切成 x.py（否則專案碰過 x.py 就誤報）
    assert P.memory_paths("留著 old.py.bak 備份") == set()
