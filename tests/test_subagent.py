"""子代理索引。

原則：216 MB 的 transcript 只抽三樣 —— 交辦內容、最後回報、產出（改檔／指令）。
中間過程不存。子代理改的檔案要併進主線統計但標記來源，不然專案卡上的
「改 N 檔」會少算（實測主線 1179、子代理 2333）。
"""

import json
import sqlite3

import indexer
import pytest
from conftest import assistant, jsonl, user_prompt


def write_agent(session_dir, agent_id, meta, rows):
    sub = session_dir / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    jsonl(sub / f"{agent_id}.jsonl", rows)
    (sub / f"{agent_id}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def with_agents(fake_home):
    session_dir = fake_home["projects"] / "slug-alpha" / "sess-1"
    write_agent(session_dir, "agent-aaa", {
        "agentType": "Explore", "description": "找出設定檔在哪",
        "toolUseId": "toolu_01", "spawnDepth": 1,
        "requestShape": "background", "model": "sonnet"
    }, [
        {"type": "user", "timestamp": "2026-09-01T10:00:10.000Z",
         "isSidechain": True, "message": {"role": "user", "content": "去找設定檔"}},
        assistant("先掃一遍。", tools=[("Grep", {"pattern": "config"})],
                  ts="2026-09-01T10:00:11.000Z"),
        assistant("在 apps/api/config 底下，共三個檔。",
                  tools=[("Edit", {"file_path": "/work/alpha/notes.md"}),
                         ("Bash", {"command": "git status --short"})],
                  ts="2026-09-01T10:00:20.000Z"),
    ])
    # 巢狀子代理：workflows 底下也要掃到
    nested = session_dir / "subagents" / "workflows" / "wf_1"
    write_agent(nested, "agent-bbb", {
        "agentType": "general-purpose", "description": "跑測試",
        "parentAgentId": "agent-aaa", "spawnDepth": 2, "model": "haiku"
    }, [
        assistant("測試全過。", tools=[("Bash", {"command": "pytest -q"})],
                  ts="2026-09-01T10:01:00.000Z"),
    ])
    indexer.run(full=True)
    return fake_home


def q(db, sql, *args):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return out


def test_兩個子代理都被索引(with_agents):
    rows = q(with_agents["db"], "SELECT agent_id, agent_type FROM subagent "
                                "ORDER BY agent_id")
    assert [r["agent_id"] for r in rows] == ["agent-aaa", "agent-bbb"]


def test_巢狀在_workflows_底下的也掃得到(with_agents):
    row = q(with_agents["db"],
            "SELECT parent_agent_id, spawn_depth FROM subagent WHERE agent_id='agent-bbb'")
    assert row[0]["parent_agent_id"] == "agent-aaa"
    assert row[0]["spawn_depth"] == 2


def test_meta_的交辦內容有進去(with_agents):
    row = q(with_agents["db"],
            "SELECT description, model, tool_use_id FROM subagent WHERE agent_id='agent-aaa'")
    assert row[0]["description"] == "找出設定檔在哪"
    assert row[0]["model"] == "sonnet"
    assert row[0]["tool_use_id"] == "toolu_01"


def test_只留最後一則發言當回報(with_agents):
    row = q(with_agents["db"], "SELECT result FROM subagent WHERE agent_id='agent-aaa'")
    assert row[0]["result"] == "在 apps/api/config 底下，共三個檔。"
    assert "先掃一遍" not in row[0]["result"], "中間過程不該被存下來"


def test_聚合值(with_agents):
    row = q(with_agents["db"],
            "SELECT turn_count, tool_count, file_count FROM subagent "
            "WHERE agent_id='agent-aaa'")[0]
    assert row["turn_count"] == 2          # 兩則 assistant
    assert row["tool_count"] == 3          # Grep + Edit + Bash
    assert row["file_count"] == 1


def test_子代理改的檔案併進主線但標記來源(with_agents):
    viaagent = q(with_agents["db"],
                 "SELECT path FROM file_touch WHERE via_agent = 'agent-aaa'")
    assert [r["path"] for r in viaagent] == ["/work/alpha/notes.md"]
    main = q(with_agents["db"], "SELECT COUNT(*) n FROM file_touch WHERE via_agent IS NULL")
    assert main[0]["n"] >= 1, "主線的改檔不該被蓋掉"


def test_子代理跑的指令也算(with_agents):
    cmds = {r["command"] for r in
            q(with_agents["db"], "SELECT command FROM command_run WHERE via_agent IS NOT NULL")}
    assert cmds == {"git status --short", "pytest -q"}


def test_重跑不會重複插入(with_agents):
    before = q(with_agents["db"], "SELECT COUNT(*) n FROM file_touch")[0]["n"]
    agents = q(with_agents["db"], "SELECT COUNT(*) n FROM subagent")[0]["n"]
    indexer.run(full=False)
    assert q(with_agents["db"], "SELECT COUNT(*) n FROM file_touch")[0]["n"] == before
    assert q(with_agents["db"], "SELECT COUNT(*) n FROM subagent")[0]["n"] == agents


def test_子代理歸到正確的專案(with_agents):
    row = q(with_agents["db"],
            "SELECT p.real_path FROM subagent a JOIN project p ON p.id = a.project_id "
            "WHERE a.agent_id = 'agent-aaa'")
    assert row and row[0]["real_path"] == str(with_agents["work"] / "alpha")


def test_回報進了全文索引(with_agents):
    hit = q(with_agents["db"],
            "SELECT a.agent_id FROM subagent_fts f JOIN subagent a ON a.id = f.rowid "
            "WHERE subagent_fts MATCH ?", '"設定檔"')
    assert [r["agent_id"] for r in hit] == ["agent-aaa"]


def test_沒有_meta_檔也不會爆(fake_home):
    session_dir = fake_home["projects"] / "slug-alpha" / "sess-1" / "subagents"
    session_dir.mkdir(parents=True, exist_ok=True)
    jsonl(session_dir / "agent-nometa.jsonl",
          [assistant("孤兒代理", ts="2026-09-01T11:00:00.000Z")])
    indexer.run(full=True)
    row = q(fake_home["db"],
            "SELECT description, result FROM subagent WHERE agent_id='agent-nometa'")
    assert row and row[0]["description"] is None
    assert row[0]["result"] == "孤兒代理"


def test_沒有子代理的環境不受影響(indexed):
    assert q(indexed["db"], "SELECT COUNT(*) n FROM subagent")[0]["n"] == 0
