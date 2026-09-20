"""JSONL / Markdown -> 結構化記錄。純函式，不碰 DB。

所有判別式的規格來源是 docs/jsonl-schema.md，改動前先讀那份。
保守存取原則：除 type/message/timestamp/sessionId/version 外一律 .get(k, default)，
樣本橫跨 7 個 CLI 版本，欄位集合差異很大。
"""

from __future__ import annotations

import json
import re

HUMAN_PROMPT_SOURCES = ("typed", "queued", "suggestion_accepted")
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}


# ── JSONL 讀取 ────────────────────────────────────────────────────────────

def iter_jsonl(path, start_offset=0):
    """逐行 yield (next_offset, record)。

    只回報「完整解析成功」那一行結束後的 offset，所以尾端半行（session 正在
    寫入時很常見）不會推進游標，下次掃描會重讀。
    """
    with open(path, "rb") as fh:
        fh.seek(start_offset)
        offset = start_offset
        for raw in fh:
            candidate = offset + len(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                offset = candidate
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                break          # 半行 -> 停住，不推進 offset
            offset = candidate
            yield offset, rec


# ── 訊息判別 ──────────────────────────────────────────────────────────────

def is_real_user_prompt(rec):
    """True 僅當這是使用者真的打字 / 排隊送出 / 點擊建議送出的內容。

    origin.kind == "human" 在 1160 筆樣本上與其餘所有訊號 bijective。
    hook 注入不在 type:"user" 內（一律是 type:"attachment"），所以這裡
    不需要任何 <system-reminder> 清洗。
    """
    if rec.get("type") != "user":
        return False
    if rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
        return False
    if (rec.get("origin") or {}).get("kind") != "human":
        return False
    if rec.get("promptSource") not in HUMAN_PROMPT_SOURCES:
        return False
    content = rec.get("message", {}).get("content")
    if not isinstance(content, str):
        return False
    return not content.lstrip().startswith("<command-name>")


def is_tool_result_echo(rec):
    if rec.get("type") != "user":
        return False
    content = rec.get("message", {}).get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def assistant_blocks(rec):
    """yield type:"assistant" 記錄裡的 content 區塊。"""
    content = rec.get("message", {}).get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def assistant_text(rec):
    parts = [b.get("text", "") for b in assistant_blocks(rec) if b.get("type") == "text"]
    return "\n".join(p for p in parts if p.strip()).strip()


def tool_uses(rec):
    for block in assistant_blocks(rec):
        if block.get("type") == "tool_use":
            yield block.get("name"), block.get("input") or {}


def has_tool_error(rec):
    """tool_result 回填是否標記了錯誤。"""
    content = rec.get("message", {}).get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")
        for b in content
    )


# ── 指令分類 ──────────────────────────────────────────────────────────────

_COMMAND_KINDS = (
    ("git", re.compile(r"\bgit\b")),
    ("test", re.compile(r"\b(pytest|jest|vitest|phpunit|go test|npm (run )?test)\b")),
    ("build", re.compile(r"\b(npm run build|vite build|tsc|webpack|make)\b")),
    ("install", re.compile(r"\b(pip install|npm (i|install)|composer install|uv add)\b")),
)

_GIT_COMMIT = re.compile(r"\bgit\s+(?:-\S+\s+|--\S+\s+)*commit\b")
# <<'EOF' / <<"EOF" / <<EOF / <<-EOF —— 收尾引號要吃掉，之前漏了這個
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\r?\n")
_M_FLAG = re.compile(r"(?:^|\s)-m\s*(?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)'|([^\s;&|]+))")
# PowerShell here-string：git commit -m @'\n<訊息>\n'@ —— 訊息在下一行，
# 不處理的話 -m 只會抓到 "@'" 這個碎片
_PS_HERESTRING = re.compile(r"(?:^|\s)-m\s*@(['\"])[^\n]*\r?\n")


def classify_command(command):
    for kind, pattern in _COMMAND_KINDS:
        if pattern.search(command):
            return kind
    return "other"


def extract_commit_messages(command):
    """從一段 shell 指令抽出 git commit 的主旨行。

    支援 `-m "..."` 與 `-F - <<'EOF' … EOF` 兩種寫法，後者是這個環境的主流
    （Bash 工具寫多行訊息只能用 heredoc）。從每個 `git commit` 出現的位置
    往後找，所以 `cd x && git add -A && git commit …` 這種串接也吃得到。
    """
    out = []
    for hit in _GIT_COMMIT.finditer(command):
        tail = command[hit.end():]
        line_end = tail.find("\n")
        first_line = tail if line_end < 0 else tail[:line_end]

        heredoc = _HEREDOC.search(tail)
        if heredoc and (line_end < 0 or heredoc.start() <= line_end):
            delim = heredoc.group(2)
            body = tail[heredoc.end():]
            end = re.search(rf"^\s*{re.escape(delim)}\s*$", body, re.M)
            if end:
                body = body[:end.start()]
            subject = next((l.strip() for l in body.splitlines() if l.strip()), "")
        else:
            ps = _PS_HERESTRING.search(tail)
            if ps and (line_end < 0 or ps.start() <= line_end):
                quote = ps.group(1)
                body = tail[ps.end():]
                end = body.find(quote + "@")
                if end >= 0:
                    body = body[:end]
                subject = next((l.strip() for l in body.splitlines() if l.strip()), "")
            else:
                flag = _M_FLAG.search(first_line)
                if not flag:
                    continue                  # --amend --no-edit 這種沒有訊息
                subject = next((g for g in flag.groups() if g is not None), "").strip()
                subject = subject.splitlines()[0].strip() if subject else ""

        if subject and subject not in out:
            out.append(subject[:500])
    return out


# ── 進度訊號 ──────────────────────────────────────────────────────────────

_AWAY_TRAILER = re.compile(r"\(disable recaps in /config\)\s*$", re.I)

# 中文 away_summary 沒有 Goal:/Next: 標記，是自由散文，但轉折詞固定。
_ZH_NEXT = re.compile(r"(下一步[是為：:，]?\s*|接下來|現在只差|再來是|還差|剩下唯一|待辦事項)")


def parse_away_summary(content):
    """`type:"system", subtype:"away_summary"` -> (goal, state, next_step)。

    兩種實測格式：
      英文 — "Goal: … Next: …"（有明確標記，goal 是真的目標）
      中文 — 自由散文「<已完成的狀態>。下一步是<…>」（前半是狀態，不是目標，
             標成目標會誤導使用者，所以分開回傳）
    """
    if not content:
        return None, None, None
    text = _AWAY_TRAILER.sub("", content).strip()
    if not text:
        return None, None, None

    goal_at, next_at = text.find("Goal:"), text.rfind("Next:")
    if goal_at >= 0 or next_at >= 0:
        goal = nxt = None
        if goal_at >= 0:
            end = next_at if next_at > goal_at else len(text)
            goal = text[goal_at + len("Goal:"):end].strip() or None
        if next_at >= 0:
            nxt = text[next_at + len("Next:"):].strip() or None
        return goal, None, nxt

    matches = list(_ZH_NEXT.finditer(text))
    if not matches:
        return None, text, None
    cut = matches[-1]
    state = text[:cut.start()].strip(" 。，、\n") or None
    marker = cut.group(0)
    tail = text[cut.start():].strip()
    if marker.startswith("下一步"):            # 這幾個字本身沒資訊，剝掉
        tail = text[cut.end():].strip(" 。，、\n")
    return None, state, tail or None


_SECTION = re.compile(r"^\s*\d+\.\s*(.+?):\s*$")
_GOAL_SECTIONS = ("primary request", "user's explicit request", "intent")
_NEXT_SECTIONS = ("next step", "pending task", "current work")


def parse_compact_summary(content):
    """`isCompactSummary` 的編號章節 -> (goal, next_step)。"""
    if not content:
        return None, None
    sections, current = {}, None
    for line in content.splitlines():
        match = _SECTION.match(line)
        if match:
            current = match.group(1).strip().lower()
            sections[current] = []
        elif current:
            sections[current].append(line.strip())

    def pick(keys):
        for name, lines in sections.items():
            if any(k in name for k in keys):
                body = " ".join(x for x in lines if x).strip()
                if body:
                    return body
        return None

    return pick(_GOAL_SECTIONS), pick(_NEXT_SECTIONS)


_MD_HEADING = re.compile(r"^#{2,3}\s+(.+?)\s*$")


def parse_memory_markdown(text):
    """brain.md / MEMORY.md 之類 -> (goal, next_step)。

    使用者的 brain.md 用 `## Focus` / `## Next (when resuming)` 這種結構。
    """
    sections, current = {}, None
    for line in text.splitlines():
        match = _MD_HEADING.match(line)
        if match:
            current = match.group(1).strip().lower()
            sections[current] = []
        elif current and line.strip() and not line.lstrip().startswith("<!--"):
            sections[current].append(line.strip())

    def pick(keys):
        for name, lines in sections.items():
            if any(name.startswith(k) for k in keys):
                body = " ".join(x for x in lines if x).strip()
                if body:
                    return body
        return None

    goal = pick(("focus", "目標", "current"))
    nxt = pick(("next", "下一步", "todo"))
    return goal, nxt
