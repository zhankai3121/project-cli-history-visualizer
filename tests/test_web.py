"""前端靜態檢查。

不開瀏覽器，但可以擋住幾個會讓版面在窄螢幕壞掉的具體寫法 ——
這些都是實際踩過的：minmax 用固定值會撐出橫向捲軸、兩個 78vh 疊起來
會變 156vh。
"""

import re
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
    encoding="utf-8")
CSS = re.search(r"<style>(.*?)</style>", HTML, re.S).group(1)
JS = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1)


def test_有_viewport_meta():
    assert 'name="viewport"' in HTML
    assert "width=device-width" in HTML


def test_大括號配對():
    assert CSS.count("{") == CSS.count("}")


def test_有_rwd_斷點():
    widths = sorted(int(w) for w in re.findall(r"@media\s*\(max-width:\s*(\d+)px\)", CSS))
    assert widths, "完全沒有 max-width 斷點"
    assert min(widths) <= 480, f"最窄斷點只到 {min(widths)}px，手機顧不到"


def test_grid_軌道不會撐出橫向捲軸():
    """minmax(20rem,1fr) 在 375px 螢幕會直接溢出，要包 min(…,100%)。"""
    for track in re.findall(r"grid-template-columns:\s*repeat\([^)]*minmax\(([^)]*)\)",
                            CSS):
        low = track.split(",")[0].strip()
        assert low.startswith("min("), f"軌道下限 {low} 沒有用 min() 夾住"


def test_split_在窄螢幕會堆疊():
    narrow = re.findall(r"@media\s*\(max-width:\s*\d+px\)\s*\{(.*?)\n\}", CSS, re.S)
    assert any(re.search(r"\.split\s*\{[^}]*grid-template-columns:\s*1fr", block)
               for block in narrow), ".split 沒有在任何斷點改成單欄"


def test_堆疊時不會兩個滿版高度相加():
    """.list 與 .pane 在桌機各自 78vh；堆疊後必須有人降下來。"""
    narrow = "\n".join(re.findall(r"@media\s*\(max-width:\s*(?:9\d\d|[0-8]\d\d)px\)\s*\{(.*?)\n\}",
                                  CSS, re.S))
    assert re.search(r"\.pane\s*\{[^}]*max-height:\s*none", narrow) or \
           re.search(r"\.list\s*\{[^}]*max-height:\s*[123]\d?vh", narrow), \
           "窄螢幕沒有處理 .list / .pane 的高度"


def test_長路徑不會撐破卡片():
    for sel in (r"\.hit", r"\.you", r"\.said"):
        block = re.search(sel + r"\s*\{([^}]*)\}", CSS)
        assert block and "overflow-wrap" in block.group(1), f"{sel} 少了 overflow-wrap"


@pytest.mark.parametrize("ident", ["fsdec", "fsinc", "theme", "reindex", "skin",
                                   "group", "sort", "q", "folders", "grid",
                                   "results", "detail", "heat", "foldermodal"])
def test_js_用到的元素都存在(ident):
    assert f'id="{ident}"' in HTML, f"JS 會抓 #{ident} 但 HTML 沒有"


def test_字級是單一旋鈕():
    """所有 font-size 都要走 rem，才可能靠 --fs 一鍵縮放。"""
    fixed = [x for x in re.findall(r"font-size:\s*([\d.]+)px", CSS)]
    assert fixed == [], f"還有寫死的 px 字級: {fixed}"
    assert "--fs:" in CSS and "font-size:var(--fs)" in CSS


def test_主題只改變數不改版面():
    """主題區塊若寫了版面規則，換主題就可能把 RWD 弄壞。"""
    for block in re.findall(r':root\[data-skin="[^"]+"\]\s*\{([^}]*)\}', CSS):
        for decl in block.split(";"):
            decl = decl.strip()
            if decl and not decl.startswith("--"):
                pytest.fail(f"主題區塊寫了非變數宣告: {decl}")
