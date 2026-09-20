"""Session 匯出 Markdown —— 端點 + 純函式。"""

import export


def test_匯出含_prompt_commit_與檔案(client):
    md = client.get("/api/session/sess-1/export.md").text
    assert "第一個問題" in md
    assert "feat: 加上設定" in md
    assert "config.py" in md
    assert "- 專案：`" in md          # real_path 由端點注入，session_detail 本身沒有
    assert "\r" not in md

    # 段落順序照 plan-v2：進度訊號 → Commits → 改過的檔案 → 指令 → 對話
    heads = [ln for ln in md.split("\n") if ln.startswith("## ")]
    assert heads == ["## 進度訊號", "## Commits", "## 改過的檔案", "## 指令", "## 對話"]

    # 對話區的 prompt 逐行都要有 blockquote 前綴
    talk = md.split("## 對話\n", 1)[1]
    assert "> 第一個問題" in talk
    assert "> _/effort_" in talk


def test_content_type_與_attachment(client):
    r = client.get("/api/session/sess-1/export.md")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert r.headers["content-disposition"] == 'attachment; filename="session-sess-1.md"'


def test_不存在的_session_404(client):
    assert client.get("/api/session/沒這個/export.md").status_code == 404


def test_純函式不做_HTML_跳脫():
    """輸出是 Markdown 不是 HTML，跳脫只會讓交接文件變難讀。"""
    md = export.session_markdown({
        "session": {"id": "s1", "title": "t"},
        "prompts": [{"ts": "2026-09-01T10:00:00Z", "text": "用 <b>粗體</b> & 符號"}],
    })
    assert "> 用 <b>粗體</b> & 符號" in md
    assert "&lt;" not in md and "&amp;" not in md


def test_指令含反引號改用波浪線圍欄():
    md = export.session_markdown({
        "session": {"id": "s1"},
        "commands": [{"kind": "other", "command": "echo '```py'"}],
    })
    assert "\n~~~\necho '```py'\n~~~\n" in md
