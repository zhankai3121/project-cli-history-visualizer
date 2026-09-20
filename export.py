"""Session 匯出 Markdown。

`session_markdown()` 是純函式，吃的就是 `server.session_detail()` 的回傳
dict —— HTTP 端點與終端機 CLI 因此共用同一份格式，不會各寫一次各長一樣。
輸出是 Markdown 不是 HTML：**一律不跳脫**，`<b>` 就照原樣印出去。
換行一律 `\n`（Windows 上也是）。
"""

from __future__ import annotations

import json

CMD_PER_KIND = 30      # 每組指令最多列幾條；長 session 的指令流水帳不值得全印
AGENT_RESULT = 1500    # 子代理回報只留開頭，完整內容在原 transcript


def session_markdown(payload: dict) -> str:
    """把一份 session 的 payload 轉成交接用的 Markdown 文件。"""
    sess = payload.get("session") or {}
    sid = sess.get("id") or ""
    lines = [f"# {_oneline(sess.get('title')) or sid[:8] or 'session'}", ""]
    lines += _meta(payload, sess, sid)
    lines += _signals(payload.get("signals") or [])
    lines += _commits(payload.get("commits") or [])
    lines += _files(payload.get("files") or [])
    lines += _commands(payload.get("commands") or [])
    lines += _talk(payload.get("prompts") or [], payload.get("turns") or [])
    lines += _agents(payload.get("subagents") or [])
    return "\n".join(lines).rstrip("\n") + "\n"


def _meta(payload, sess, sid):
    out = []
    real_path = (payload.get("project") or {}).get("real_path")
    if real_path:
        out.append(f"- 專案：`{real_path}`")
    span = " ~ ".join(x for x in (_dt(sess.get("started_at")),
                                 _dt(sess.get("ended_at"))) if x)
    if span:
        out.append(f"- 期間：{span}")
    # codex 沒有 --resume 這回事，印了只會誤導
    if sid and (sess.get("tool") or "claude") == "claude":
        out.append(f"- 續接：`claude --resume {sid}`")
    return out + [""] if out else []


def _signals(signals):
    keep = [s for s in signals if s.get("goal") or s.get("state") or s.get("next_step")]
    if not keep:
        return []
    out = ["## 進度訊號", ""]
    for s in keep:
        for label, key in (("目標", "goal"), ("狀態", "state"), ("下一步", "next_step")):
            if s.get(key):
                out.append(f"- **{label}** {_oneline(s[key])}")
        out.append(f"- 來源：{s.get('origin') or s.get('kind') or ''} · {_dt(s.get('ts'))}")
        out.append("")
    return out


def _commits(commits):
    if not commits:
        return []
    out = ["## Commits", ""]
    out += [f"- {_dt(c.get('ts'))} {_oneline(c.get('message'))}".strip() for c in commits]
    return out + [""]


def _files(files):
    if not files:
        return []
    out = ["## 改過的檔案", ""]
    for f in files:
        # 路徑用反引號包住，Windows 的反斜線才不會被 Markdown 當成跳脫
        line = f"- `{f.get('path') or ''}` ×{f.get('n') or 1}"
        extra = [x for x in (f.get("verb"), "子代理" if f.get("via_agent") else None) if x]
        out.append(line + (" · " + " · ".join(extra) if extra else ""))
    return out + [""]


def _commands(commands):
    if not commands:
        return []
    groups = {}
    for c in commands:
        groups.setdefault(c.get("kind") or "other", []).append(c.get("command") or "")
    out = ["## 指令", ""]
    for kind, cmds in groups.items():
        shown = cmds[:CMD_PER_KIND]
        # 指令自己含 ``` 的話（heredoc 貼 markdown 很常見）圍欄要換成 ~~~
        fence = "~~~" if any("```" in c for c in shown) else "```"
        out += [f"### {kind}", "", fence, *shown, fence, ""]
        if len(cmds) > len(shown):
            out += [f"（另有 {len(cmds) - len(shown)} 條沒列出）", ""]
    return out


def _talk(prompts, turns):
    if not prompts:
        return []
    out = ["## 對話", ""]
    for i, p in enumerate(prompts):
        nxt = prompts[i + 1] if i + 1 < len(prompts) else None
        out += _quote(p)
        said, tools = _window(turns, p.get("ts"), nxt.get("ts") if nxt else None)
        if said:
            out += [said, ""]
        if tools:
            out += ["工具：" + " · ".join(f"`{t}`" for t in tools), ""]
    return out


def _quote(p):
    text = p.get("text") or ""
    if p.get("is_slash"):
        text = f"_{text.strip()}_"
    return [f"> {line}" for line in text.split("\n")] + [""]


def _window(turns, start, end):
    """prompt.ts ≤ turn.ts < 下一則 prompt.ts —— 與 index.html promptBlock 同邏輯。"""
    end = end or "9999"
    within = [t for t in turns
              if t.get("ts") and (not start or t["ts"] >= start) and t["ts"] < end]
    said = [t["assistant_summary"] for t in within if t.get("assistant_summary")]
    tools = []
    for t in within:
        for name in json.loads(t.get("tools_json") or "[]"):
            if name not in tools:
                tools.append(name)
    return (said[-1] if said else ""), tools


def _agents(agents):
    if not agents:
        return []
    out = ["## 子代理", ""]
    for a in agents:
        title = a.get("description") or a.get("agent_type") or a.get("agent_id") or ""
        out += [f"### {_oneline(title)}", ""]
        bits = [x for x in (a.get("agent_type"), a.get("model")) if x]
        bits.append(f"{a.get('turn_count') or 0} turns · {a.get('tool_count') or 0} 工具")
        out += ["- " + " · ".join(bits), ""]
        result = (a.get("result") or "").strip()
        if result:
            out += [result[:AGENT_RESULT] + ("…" if len(result) > AGENT_RESULT else ""), ""]
    return out


def _dt(ts):
    return (ts or "")[:16].replace("T", " ")


def _oneline(text):
    return " ".join((text or "").split())
