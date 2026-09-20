"""前端靜態檢查。

不開瀏覽器，但擋得住幾個實際踩過的寫法錯誤。

寫這類測試最大的風險是「空轉通過」—— regex 找不到東西時迴圈不執行，
測試照樣綠燈，該擋的 bug 回來了也沒人知道。所以下面每一條只要是掃描型的，
都會先斷言「至少掃到 N 筆」。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
RAW_CSS = re.search(r"<style>(.*?)</style>", HTML, re.S).group(1)
RAW_JS = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1)


def strip_comments(text, block=True):
    """拿掉 /* */ 註解。註解裡的大括號與分號會汙染所有文字層級的檢查。"""
    out = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return out


CSS = strip_comments(RAW_CSS)


def decls(body):
    """一段規則的內容 -> [(屬性, 值)]，已略過註解與空片段。"""
    got = []
    for chunk in body.split(";"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        prop, _, value = chunk.partition(":")
        got.append((prop.strip().lower(), value.strip()))
    return got


def balanced(text, start):
    """從 start（指向 '('）往後找配對的右括號，回傳內容。"""
    depth, i = 0, start
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    return ""


FIXED_LEN = re.compile(r"^[\d.]+(rem|em|px|ch|pt)$")


def minmax_tracks():
    """所有 minmax(…) 的內容，括號正確配對（內層還有 min()）。"""
    return [balanced(CSS, m.start() + len("minmax"))
            for m in re.finditer(r"\bminmax(?=\()", CSS)]


def repeat_tracks():
    """repeat(auto-fill/auto-fit, minmax(…)) 裡的 minmax 內容。

    只有這種會隨視窗寬度長出多欄，下限沒夾住就會撐出橫向捲軸。
    固定欄數的側欄（.split）是另一回事，看下面那條測試。
    """
    out = []
    for m in re.finditer(r"\brepeat(?=\()", CSS):
        body = balanced(CSS, m.start() + len("repeat"))
        if "auto-fill" not in body and "auto-fit" not in body:
            continue
        inner = re.search(r"\bminmax(?=\()", body)
        if inner:
            out.append(balanced(body, inner.start() + len("minmax")))
    return out


def split_top(text):
    """依最外層逗號切開。"""
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts]


# ── viewport 與斷點 ───────────────────────────────────────────────────────

def test_有真的_viewport_meta():
    """子字串比對會在標籤被註解掉時照樣通過，所以比對真正的 meta 標籤。"""
    body = re.sub(r"<!--.*?-->", "", HTML, flags=re.S)
    tag = re.search(r"<meta[^>]*name=[\"']viewport[\"'][^>]*>", body, re.I)
    assert tag, "沒有有效的 viewport meta（註解掉的不算）"
    assert re.search(r"content=[\"'][^\"']*width\s*=\s*device-width", tag.group(0), re.I)


def test_斷點存在且有內容():
    blocks = re.findall(r"@media\s*\(max-width:\s*(\d+)px\)\s*\{", CSS)
    assert blocks, "完全沒有 max-width 斷點"
    widths = sorted(int(w) for w in blocks)
    assert min(widths) <= 480, f"最窄斷點只到 {min(widths)}px，手機顧不到"
    # 空的 @media 也能讓上面那條通過，所以檢查每個區塊真的有宣告
    for m in re.finditer(r"@media\s*\(max-width:\s*\d+px\)\s*\{", CSS):
        body = balanced("(" + CSS[m.end() - 1:], 0) if False else None
        tail = CSS[m.end():]
        depth, i = 1, 0
        while i < len(tail) and depth:
            depth += (tail[i] == "{") - (tail[i] == "}")
            i += 1
        assert ":" in tail[:i], "有空的 @media 區塊"


# ── 版面：窄螢幕不能橫向溢出 ──────────────────────────────────────────────

def test_自動排版軌道的下限會被視窗夾住():
    """minmax(20rem,1fr) 在 375px 會撐出橫向捲軸，下限必須被 100% 夾住。"""
    tracks = repeat_tracks()
    assert tracks, "一個 repeat(auto-fill/fit, minmax(…)) 都沒掃到，測試會空轉"
    bad = []
    for track in tracks:
        low = split_top(track)[0]
        if low.startswith("min("):
            inner = split_top(balanced(low, low.index("(")))
            # min(20rem) 沒有第二個參數等於沒夾，要有百分比上限才算數
            if not any("%" in x for x in inner[1:]):
                bad.append(f"{low}（min() 裡沒有百分比上限，等於沒夾）")
        elif FIXED_LEN.match(low):
            bad.append(low)
    assert bad == [], f"這些自動軌道的下限是固定值，窄螢幕會溢出: {bad}"


def test_固定寬欄位一定要有單欄退路():
    """任何固定欄數 + 固定寬度的 grid，窄螢幕都必須能塌成單欄。"""
    all_cols = [(m.group(1), v) for m in re.finditer(r"([.#][\w-]+)\s*\{([^}]*)\}", CSS)
                for p, v in decls(m.group(2)) if p == "grid-template-columns"]
    assert all_cols, "一個 grid-template-columns 都沒掃到，測試會空轉"

    risky = {sel for sel, value in all_cols
             if "repeat(" not in value
             and any("minmax(" in part or FIXED_LEN.match(part)
                     for part in value.split())}
    narrow = media_body(max_w=999)
    for sel in risky:
        assert re.search(re.escape(sel) + r"\s*\{[^}]*grid-template-columns:\s*1fr",
                         narrow), f"{sel} 用了固定寬欄位，但沒有斷點讓它塌成單欄"


def media_body(min_w=0, max_w=10_000):
    """把符合寬度範圍的 max-width 區塊內容串起來。"""
    out = []
    for m in re.finditer(r"@media\s*\(max-width:\s*(\d+)px\)\s*\{", CSS):
        if not (min_w <= int(m.group(1)) <= max_w):
            continue
        tail = CSS[m.end():]
        depth, i = 1, 0
        while i < len(tail) and depth:
            depth += (tail[i] == "{") - (tail[i] == "}")
            i += 1
        out.append(tail[:i])
    return "".join(out)


def test_split_在窄螢幕會堆疊():
    """兩種機制都接受：flex-wrap 自然塌陷，或斷點改單欄。"""
    m = re.search(r"\.split\s*\{([^}]*)\}", CSS)
    assert m, "找不到 .split"
    props = dict(decls(m.group(1)))

    if props.get("display") == "flex" and props.get("flex-wrap") == "wrap":
        # 靠 flex-wrap 塌陷的話，子項一定要有 flex-basis，否則永遠不換行
        for sel in (".list", ".pane"):
            child = dict(decls(re.search(re.escape(sel) + r"\s*\{([^}]*)\}",
                                         CSS).group(1)))
            assert "flex" in child or "flex-basis" in child, \
                f"{sel} 沒有 flex-basis，.split 不會換行"
            assert child.get("min-width") == "0", \
                f"{sel} 沒有 min-width:0，長內容會撐破 flex 容器"
        return

    assert re.search(r"\.split\s*\{[^}]*grid-template-columns:\s*1fr",
                     media_body(max_w=999)), \
        ".split 既沒用 flex-wrap，也沒有任何斷點改成單欄"


def flex_basis_rem(selector):
    body = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS).group(1)
    value = dict(decls(body)).get("flex", "")
    hit = re.search(r"([\d.]+)rem", value)
    return float(hit.group(1)) if hit else None


def test_側欄基準總和要對上堆疊斷點():
    """.split 靠 flex-wrap 塌陷，塌陷時機由兩個 flex-basis 的總和決定。

    總和 42rem 換算約 906px，剛好對上 900px 的高度斷點。調側欄寬度時
    只搬比例、不動總和，否則堆疊點會飄離斷點，出現「已經很窄卻還並排」
    或「明明放得下卻堆疊」的中間地帶。
    """
    lst, pane = flex_basis_rem(".list"), flex_basis_rem(".pane")
    assert lst and pane, "flex-basis 抓不到，寫法可能改了"
    assert lst + pane == pytest.approx(42, abs=0.5), \
        f"基準總和 {lst}+{pane}={lst + pane}rem，偏離 42rem 會讓堆疊點離開 900px 斷點"


def test_側欄不要跟著變寬():
    """session 列只有標題加一行摘要，長到 1920px 的三分之一是浪費。"""
    body = re.search(r"\.list\s*\{([^}]*)\}", CSS).group(1)
    grow = dict(decls(body)).get("flex", "").split()[0]
    assert grow == "0", f".list 的 flex-grow 是 {grow}，寬螢幕會長得太寬"


def test_堆疊之後側欄要吃滿整行():
    narrow = media_body(max_w=999)
    assert re.search(r"\.list\s*\{[^}]*flex-basis:\s*100%", narrow), \
        "堆疊後 .list 還是 14rem，右邊會空一大塊"


def test_堆疊時不會兩個滿版高度相加():
    narrow = media_body(max_w=999)
    assert re.search(r"\.pane\s*\{[^}]*max-height:\s*none", narrow) or \
           re.search(r"\.list\s*\{[^}]*max-height:\s*[1-4]?\dvh", narrow), \
           "窄螢幕沒有處理 .list / .pane 的高度"


def test_會出現長路徑的地方都能斷字():
    for sel in (".hit", ".you", ".said", ".note"):
        m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
        assert m, f"找不到 {sel} 規則"
        props = dict(decls(m.group(1)))
        assert "overflow-wrap" in props or "word-break" in props, \
            f"{sel} 少了斷字規則，長路徑會撐破版面"


def test_次要工具列黏在_header_下方():
    """chips / 回總覽 / 搜尋範圍往下滾時要還按得到。"""
    for sel in (".chips", ".scopebar", ".subbar"):
        block = None
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", CSS):
            if sel in m.group(1) and "position:sticky" in m.group(2):
                block = dict(decls(m.group(2)))
                break
        assert block, f"{sel} 沒有 position:sticky"
        assert "var(--head-h" in block.get("top", ""), \
            f"{sel} 的 top 沒有吃 --head-h，header 換行後會錯位"
        assert block.get("background"), f"{sel} 沒有背景，捲動時內容會透出來"


def test_head_h_由_js_量出來():
    """header 會隨視窗寬度換行，高度寫死一定會錯。"""
    assert re.search(r'setProperty\(\s*["\']--head-h["\']', RAW_JS), \
        "JS 沒有實際設定 --head-h"
    assert re.search(r"new\s+ResizeObserver\([^)]*\)\s*\.observe\(", RAW_JS), \
        "沒有真的掛上 ResizeObserver —— 字級或主題改變時高度不會重算"


def test_黏住的列與_main_內距用同一個變數():
    """負 margin 出血到邊緣，兩邊對不上的話斷點一換就會露出縫。"""
    assert re.search(r"main\s*\{[^}]*padding:[^;}]*var\(--main-pad\)", CSS)
    sticky = re.search(r"\.subbar[^{]*\{([^}]*position:sticky[^}]*)\}", CSS).group(1)
    margin = dict(decls(sticky)).get("margin", "")
    assert "var(--main-pad)" in margin, \
        f"黏住的列的 margin 沒有吃 --main-pad，斷點一換就對不齊: {margin!r}"


def test_sticky_層級低於_header():
    head_z = int(re.search(r"header\s*\{[^}]*z-index:\s*(\d+)", CSS).group(1))
    sub_z = int(re.search(r"\.subbar[^{]*\{[^}]*z-index:\s*(\d+)", CSS).group(1))
    assert sub_z < head_z, "次要工具列會蓋到 header"


def test_不要用_order_打亂_tab_順序():
    """視覺順序與 DOM 順序不一致會讓鍵盤使用者跳來跳去（WCAG 2.4.3）。"""
    for prop, value in [d for m in re.finditer(r"\{([^}]*)\}", CSS)
                        for d in decls(m.group(1))]:
        assert prop != "order", f"用了 order:{value}，會desync tab 順序"


def test_觸控查詢要用_any_pointer():
    """pointer 只看主要指標裝置，觸控筆電回報 fine，會漏掉。"""
    assert "any-pointer: coarse" in CSS or "any-pointer:coarse" in CSS
    bare = re.findall(r"@media\s*\(\s*pointer:\s*coarse", CSS)
    assert bare == [], "用了 (pointer: coarse)，觸控筆電會漏掉"


# ── 字級：--fs 是唯一旋鈕 ─────────────────────────────────────────────────

def test_沒有寫死的字級():
    """font-size 與 font 簡寫都要檢查 —— 只看 font-size 會漏掉簡寫裡的 px。"""
    bad = []
    for m in re.finditer(r"\{([^}]*)\}", CSS):
        for prop, value in decls(m.group(1)):
            if prop == "font-size" and re.search(r"\d(px|pt)\b", value):
                bad.append(f"font-size:{value}")
            elif prop == "font" and re.search(r"(^|\s)[\d.]+(px|pt)\b", value):
                bad.append(f"font:{value}")
            elif prop in ("font-size", "font") and "em" in value and "rem" not in value:
                bad.append(f"{prop}:{value}（em 會被父層縮放，脫離 --fs）")
    # --fs 自己是 px，那是旋鈕本身，不算
    bad = [b for b in bad if "var(--fs)" not in b]
    assert bad == [], f"這些字級沒走 rem/var(--fs): {bad}"


def test_字級旋鈕有接上():
    assert re.search(r"--fs\s*:", CSS)
    assert re.search(r"html\s*\{[^}]*font-size:\s*var\(\s*--fs\s*\)", CSS), \
        "html 沒有吃 var(--fs)"


def test_字級用_clamp_連續縮放而不是階梯():
    """階梯會在跨斷點時讓字突然跳一階；clamp 是連續的。"""
    # CSS 裡有多個 :root{} 區塊（色票一個、字級一個），全部合起來看
    root = "".join(m.group(1) for m in re.finditer(r":root\s*\{([^}]*)\}", CSS))
    fs = dict(decls(root)).get("--fs", "")
    assert "clamp(" in fs, f"--fs 不是 clamp(): {fs}"
    assert "vw" in fs, "clamp 中段沒有 vw，等於沒有隨視窗縮放"
    assert "var(--fs-scale" in fs, "--fs 沒有吃使用者倍率"
    # 斷點裡不該再出現 --fs，否則又變回階梯
    assert "--fs:" not in media_body(), "媒體查詢裡還有 --fs，階梯沒清乾淨"


def test_字級倍率是相對值不是絕對_px():
    """存絕對 px 的話，在寬螢幕調過之後換到手機會黏著不放。"""
    assert "clihv-fs-scale" in RAW_JS
    assert "--fs-scale" in RAW_JS
    assert not re.search(r'setProperty\(\s*"--fs"\s*,', RAW_JS), \
        "JS 直接覆寫 --fs，會蓋掉 clamp() 的響應式行為"


# ── 主題不准碰版面 ────────────────────────────────────────────────────────

# 真正的不變量不是「主題只能寫變數」—— 實際有 7 條 descendant 規則在改
# text-shadow / padding / transform，那些是刻意的視覺差異。
# 要守的是：主題不能動 RWD 賴以運作的版面原語。
LAYOUT_PROPS = {
    "display", "position", "float", "flex", "flex-direction", "flex-wrap",
    "grid", "grid-template", "grid-template-columns", "grid-template-rows",
    "grid-auto-flow", "grid-auto-columns", "grid-auto-rows",
    "width", "min-width", "max-width", "height", "min-height", "max-height",
    "overflow", "overflow-x", "overflow-y", "top", "right", "bottom", "left",
    "inset", "columns", "column-count",
}


def skin_rules():
    """所有主題規則（裸區塊 + descendant），回傳 (選擇器, 內容)。"""
    return [(m.group(1), m.group(2)) for m in
            re.finditer(r'(:root\[data-skin="[^"]+"\][^{]*)\{([^}]*)\}', CSS)]


def test_有掃到主題規則():
    rules = skin_rules()
    bare = [s for s, _ in rules if s.rstrip().endswith("]")]
    desc = [s for s, _ in rules if not s.rstrip().endswith("]")]
    assert len(bare) >= 10, f"只掃到 {len(bare)} 個主題，regex 失效了"
    assert desc, "descendant 主題規則一條都沒掃到，這個測試會空轉"


@pytest.mark.parametrize("selector,body", skin_rules())
def test_主題不碰版面原語(selector, body):
    for prop, value in decls(body):
        assert prop not in LAYOUT_PROPS, \
            f"{selector.strip()} 設了版面屬性 {prop}:{value}，換這個主題會把 RWD 弄壞"


def test_主題沒有覆寫字級旋鈕():
    for selector, body in skin_rules():
        assert "--fs" not in body, f"{selector.strip()} 動了 --fs"


# ── 結構完整性 ────────────────────────────────────────────────────────────

def test_大括號配對():
    assert CSS.count("{") == CSS.count("}"), "CSS 大括號不配對（已排除註解）"


JS_IDS = sorted(set(re.findall(r'\$\("#([\w-]+)"\)', RAW_JS))
                | set(re.findall(r'getElementById\("([\w-]+)"\)', RAW_JS)))


def test_有掃到_js_用的_id():
    assert len(JS_IDS) >= 20, f"只掃到 {len(JS_IDS)} 個 id，regex 失效了"


@pytest.mark.parametrize("ident", JS_IDS)
def test_js_抓的_id_都存在(ident):
    """清單從 JS 自動推導，不是手寫 —— 手寫的清單永遠會跟不上程式。"""
    assert re.search(rf'id=["\']{re.escape(ident)}["\']', HTML), \
        f"JS 會抓 #{ident} 但 HTML 沒有這個元素"
