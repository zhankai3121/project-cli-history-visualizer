"""終端機介面 —— 直接呼叫 cli.main()，看 exit code 與印出來的字。"""

import io
import json
import sys

import cli


def test_cli_search_印出命中(indexed, capsys):
    code = cli.main(["search", "第一個問題"])
    out = capsys.readouterr().out
    assert code == 0
    assert "alpha" in out
    assert "第一個問題" in out


def test_cli_json_輸出可解析(indexed, capsys):
    code = cli.main(["search", "第一個問題", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["count"] >= 1


def test_cli_無結果_exit_1(indexed, capsys):
    code = cli.main(["search", "絕對不存在的字串zzz"])
    assert code == 1
    assert capsys.readouterr().out == ""


def test_cli_projects_列出專案(indexed, capsys):
    code = cli.main(["projects"])
    assert code == 0
    assert "alpha" in capsys.readouterr().out


def test_cli_recent_列出最近的_prompt(indexed, capsys):
    code = cli.main(["recent", "--limit", "5"])
    assert code == 0
    assert "第一個問題" in capsys.readouterr().out


def test_cli_session_不存在_exit_1(indexed, capsys):
    code = cli.main(["session", "沒這個 id"])
    assert code == 1
    assert "找不到" in capsys.readouterr().err


def test_cli_session_印出_prompt(indexed, capsys):
    code = cli.main(["session", "sess-1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "claude --resume sess-1" in out
    assert "第一個問題" in out


def test_cli_md_沒有_export_模組就提示(indexed, capsys, monkeypatch):
    """F6 的 export.py 還沒實作（或被拔掉）時，要給一行提示而不是 traceback。"""
    monkeypatch.setitem(sys.modules, "export", None)   # 強制 ImportError
    code = cli.main(["session", "sess-1", "--md"])
    assert code == 1
    assert "export.py" in capsys.readouterr().err


def test_cli_沒有索引_exit_2(fake_home, capsys):
    code = cli.main(["search", "隨便"])
    assert code == 2
    assert "先跑 python indexer.py" in capsys.readouterr().err


def test_cli_utf8_在_ascii_stdout_不炸(indexed, monkeypatch):
    """Big5／ASCII 主控台下印中文會 UnicodeEncodeError，main() 第一行要先轉碼。"""
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="ascii", errors="replace")
    monkeypatch.setattr(sys, "stdout", stream)
    code = cli.main(["search", "第一個問題"])
    stream.flush()
    stream.detach()
    assert code == 0
    assert "第一個問題" in buf.getvalue().decode("utf-8")


def test_cli_沒有_reconfigure_的串流不會爆(indexed):
    class Dumb:
        pass

    cli.utf8(Dumb())          # 不該丟例外
