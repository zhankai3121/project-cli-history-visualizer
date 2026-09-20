"""Token / cost 統計。

最重要的一條：**一次 API 呼叫會被拆成好幾行 assistant**（text 一行、tool_use
一行），而且 usage 完全相同。本機 3867 行只對應 1621 個 requestId —— 不依
requestId 去重，帳單會被高估 2.4 倍。`model:"<synthetic>"` 是 CLI 自己合成的
錯誤訊息，根本沒打過 API，也要跳掉。
"""

import json
import sqlite3

import indexer
import pytest
from conftest import assistant, jsonl


def usage_line(text, request_id, uuid, model="claude-opus-5",
               ts="2026-09-01T10:05:00.000Z", inp=100, cc=200, cr=5000,
               out=50, thinking=10):
    rec = assistant(text, ts=ts)
    rec["uuid"] = uuid
    rec["requestId"] = request_id
    rec["message"]["model"] = model
    rec["message"]["usage"] = {
        "input_tokens": inp,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
        "output_tokens": out,
        "output_tokens_details": {"thinking_tokens": thinking},
    }
    return rec


def append(path, records):
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def q(db, sql, *args):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return out


@pytest.fixture
def with_usage(fake_home):
    """同一次呼叫兩行、另一次呼叫一行，外加一行合成的錯誤訊息。"""
    append(fake_home["projects"] / "slug-alpha" / "sess-1.jsonl", [
        usage_line("我想一下。", "req-1", "u-a"),                    # 同一次呼叫
        usage_line("順手改個檔。", "req-1", "u-b"),                  # 的第二行
        usage_line("改好了。", "req-2", "u-c", model="claude-fable-5-1",
                   inp=10, cc=20, cr=30, out=40, thinking=0),
        usage_line("API Error: overloaded", "req-syn", "u-d",
                   model="<synthetic>", inp=1, cc=1, cr=1, out=1, thinking=1),
    ])
    indexer.run(full=True)
    return fake_home


@pytest.fixture
def tok_client(with_usage):
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


def test_usage_依_requestId_去重(with_usage):
    rows = q(with_usage["db"], "SELECT request_id, input_tokens, output_tokens, "
                               "cache_create_tokens, cache_read_tokens, thinking_tokens "
                               "FROM api_call WHERE request_id = 'req-1'")
    assert len(rows) == 1, "同一個 requestId 的兩行 assistant 被算成兩次呼叫"
    assert rows[0]["input_tokens"] == 100
    assert rows[0]["cache_create_tokens"] == 200
    assert rows[0]["cache_read_tokens"] == 5000
    assert rows[0]["output_tokens"] == 50
    assert rows[0]["thinking_tokens"] == 10


def test_synthetic_模型跳過(with_usage):
    assert q(with_usage["db"],
             "SELECT COUNT(*) n FROM api_call WHERE request_id = 'req-syn'")[0]["n"] == 0
    assert q(with_usage["db"],
             "SELECT COUNT(*) n FROM api_call")[0]["n"] == 2


def test_專案_token_聚合_進_overview(tok_client):
    projects = tok_client.get("/api/overview").json()["projects"]
    hit = [p for p in projects if p["tokens_all"]]
    assert hit, "overview 沒有任何專案帶 token 數"
    assert hit[0]["tokens_out"] == 90              # 50 + 40
    assert hit[0]["tokens_all"] == 420             # (100+200+50) + (10+20+40)

    detail = tok_client.get(f"/api/project/{hit[0]['id']}/tokens").json()
    assert detail["total"] == {"calls": 2, "input": 110, "cache_create": 220,
                               "cache_read": 5030, "output": 90, "thinking": 10}
    assert {m["model"] for m in detail["models"]} == {"claude-opus-5", "claude-fable-5-1"}
    assert detail["by_day"] and detail["by_day"][0]["day"] == "2026-09-01"
    assert set(detail["agents"]) == {"output", "input"}

    usage = tok_client.get("/api/session/sess-1").json()["usage"]
    assert usage["calls"] == 2 and usage["output"] == 90
    assert len(usage["models"]) == 2


def test_heatmap_metric_tokens(tok_client):
    prompts = tok_client.get("/api/heatmap").json()
    tokens = tok_client.get("/api/heatmap?metric=tokens").json()
    assert prompts["metric"] == "prompts" and tokens["metric"] == "tokens"
    assert {d["day"] for d in tokens["days"]} >= {d["day"] for d in prompts["days"]}, \
        "有 prompt 的日子在 tokens 模式下不見了"
    day = next(d for d in tokens["days"] if d["day"] == "2026-09-01")
    assert day["n"] == 420, "熱度要用 input+cache_create+output"
    assert day["out"] == 90
    assert day["cache_read"] == 5030


def test_舊DB_會回填_usage(with_usage):
    """舊 DB 的 transcript 已讀到檔尾，不跑 --full 也要補得回來。"""
    db = with_usage["db"]
    turns = q(db, "SELECT COUNT(*) n FROM turn")[0]["n"]
    con = sqlite3.connect(db)
    con.execute("DELETE FROM api_call")
    con.execute("DELETE FROM app_config WHERE key = 'usage_backfilled'")
    con.commit()
    con.close()

    indexer.run(full=False)
    assert q(db, "SELECT COUNT(*) n FROM api_call")[0]["n"] == 2
    assert q(db, "SELECT COUNT(*) n FROM turn")[0]["n"] == turns, \
        "回填不該碰 scan_state —— turn 沒有 UNIQUE，重讀會整批重複"


def test_子代理_usage_去重(fake_home):
    sub = fake_home["projects"] / "slug-alpha" / "sess-1" / "subagents"
    jsonl(sub / "agent-tok.jsonl", [
        usage_line("先掃一遍。", "req-s1", "s-a", out=10, thinking=0),
        usage_line("再補一句。", "req-s1", "s-b", out=10, thinking=0),
        usage_line("回報。", "req-s2", "s-c", inp=7, cc=8, cr=9, out=7, thinking=0),
    ])
    indexer.run(full=True)
    row = q(fake_home["db"],
            "SELECT input_tokens, output_tokens, cache_create_tokens, "
            "cache_read_tokens FROM subagent WHERE agent_id = 'agent-tok'")[0]
    assert row["output_tokens"] == 17, "同一個 requestId 的兩行被加了兩次"
    assert row["input_tokens"] == 107
    assert row["cache_create_tokens"] == 208
    assert row["cache_read_tokens"] == 5009
