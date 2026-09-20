"""CLI History Visualizer —— 終端機介面。

    python cli.py search 設定檔 --scope prompt
    python cli.py projects
    python cli.py session <id> [--md]
    python cli.py recent --limit 20

不走 HTTP、不開瀏覽器：server.py 的端點就是普通函式，直接呼叫。
exit code：有輸出 0、查無結果 1、還沒建索引 2。
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys

import indexer
import server

NO_DB = "找不到索引資料庫，先跑 python indexer.py"


def utf8(stream):
    """Big5 主控台會把中文輸出炸成 UnicodeEncodeError —— 先轉成 UTF-8。

    測試會把 sys.stdout 換成別的物件，不保證有 reconfigure，所以要擋。
    """
    fn = getattr(stream, "reconfigure", None)
    if fn is None:
        return
    try:
        fn(encoding="utf-8", errors="replace")
    except (ValueError, OSError):       # 已經讀過的串流不給改，不是致命錯誤
        pass


def warn(msg):
    print(msg, file=sys.stderr)


def flat(text, limit=120):
    """壓成一行再截斷 —— prompt 常常是好幾段。"""
    one = " ".join((text or "").split())
    return one[:limit] + ("…" if len(one) > limit else "")


def dump(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def hit_line(hit):
    where = hit.get("display_name") or "?"
    title = hit.get("title")
    head = f"{where} › {flat(title, 40)}" if title else where
    return f"{(hit.get('ts') or '')[:10]}  {head}  {flat(hit.get('text'))}"


def show_hits(hits):
    for hit in hits:
        print(hit_line(hit))
    return 0 if hits else 1


def cmd_search(args):
    kwargs = {"q": " ".join(args.q).strip(), "limit": args.limit, "scope": args.scope}
    if not kwargs["q"]:
        warn("關鍵字不可為空")
        return 2
    if args.since or args.until:
        # F8 之前 search() 沒有這兩個參數，硬傳會 TypeError
        params = inspect.signature(server.search).parameters
        if "since" in params and "until" in params:
            kwargs["since"], kwargs["until"] = args.since or "", args.until or ""
        else:
            warn("這個版本的 search 還不吃 --since/--until，已忽略")
    data = server.search(**kwargs)
    if args.json:
        dump(data)
        return 0 if data["hits"] else 1
    return show_hits(data["hits"])


def cmd_projects(args):
    data = server.overview()
    projects = data["projects"]
    if args.json:
        dump(data)
        return 0 if projects else 1
    for proj in projects:
        note = proj.get("goal") or proj.get("next_step") or proj.get("state")
        print(f"{(proj['last_seen'] or '')[:10]}  {proj['display_name']}  "
              f"{proj['prompt_count']} prompts  {flat(note, 60)}".rstrip())
    return 0 if projects else 1


def cmd_session(args):
    try:
        payload = server.session_detail(args.id)
    except server.HTTPException:            # 端點用 404 表示「沒這個 session」
        warn(f"找不到 session：{args.id}")
        return 1

    if args.md:
        try:
            from export import session_markdown
        except ImportError:
            warn("--md 需要 export.py（F6 Markdown 匯出），這份還沒實作")
            return 1
        print(session_markdown(payload))
        return 0

    if args.json:
        dump(payload)
        return 0

    sess = payload["session"]
    print(f"{sess.get('title') or sess['id']}  ({sess['id']})")
    print(f"{(sess.get('started_at') or '')[:16]}  {sess['prompt_count']} prompts  "
          f"{len(payload['files'])} 檔  {len(payload['commits'])} commits")
    if sess.get("tool") == "claude":
        print(f"claude --resume {sess['id']}")
    for prompt in payload["prompts"]:
        print(f"  {(prompt['ts'] or '')[11:16]}  {flat(prompt['text'])}")
    return 0


def cmd_recent(args):
    return show_hits(server.recent(limit=args.limit)["hits"])


def build_parser():
    ap = argparse.ArgumentParser(prog="cli.py", description="在終端機查 CLI 歷史索引")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="搜尋 prompt / 回覆 / 子代理")
    s.add_argument("q", nargs="+", help="關鍵字")
    s.add_argument("--scope", default="all", choices=["all", "prompt", "reply", "agent"])
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--since", help="YYYY-MM-DD（需要 F8 的搜尋參數）")
    s.add_argument("--until", help="YYYY-MM-DD（需要 F8 的搜尋參數）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    p = sub.add_parser("projects", help="列出專案")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_projects)

    d = sub.add_parser("session", help="一次對話的內容")
    d.add_argument("id")
    d.add_argument("--md", action="store_true", help="輸出 Markdown（需要 export.py）")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_session)

    r = sub.add_parser("recent", help="最近的 prompt")
    r.add_argument("--limit", type=int, default=30)
    r.set_defaults(fn=cmd_recent)
    return ap


def main(argv=None):
    utf8(sys.stdout)
    utf8(sys.stderr)
    args = build_parser().parse_args(argv)
    if not indexer.DB_PATH.exists():        # 路徑不快取：測試會換掉 DB_PATH
        warn(NO_DB)
        return 2
    return args.fn(args)


if __name__ == "__main__":
    try:
        code = main()
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        # 接 head / findstr 之類提早關管線：Windows 沒有 SIGPIPE，直接靜默結束
        os._exit(0)
    sys.exit(code)
