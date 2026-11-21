"""界面不变量回归测试。

覆盖三类曾经出现过的问题：
1. 悬浮球被拖动后永久离开右边缘（历史问题，规则仍在，防止回归）；
2. 语音助手从浮窗改为固定右栏后，不能再出现 fixed 定位 / 位移 / 遮挡播放器；
3. 右栏与让位规则的一致性。
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def _css() -> str:
    return (WEB / "style.css").read_text(encoding="utf-8")


def _js() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


def _blocks(css: str, selector: str) -> list[str]:
    return [m.group(1) for m in re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", css)]


def test_app_shell_has_three_columns():
    css = _css()
    shell = _blocks(css, ".app-shell")[0]
    assert "--assistant-w" in shell, "缺少右栏宽度变量"
    assert "grid-template-columns" in shell, "缺少栅格定义"
    assert shell.count("minmax(0, 1fr)") >= 1 and "var(--assistant-w)" in shell, "右栏应是栅格的第三列"


def test_assistant_is_docked_not_floating():
    drawers = [b for b in _blocks(_css(), ".chat-panel.drawer") if "position" in b]
    assert drawers, "找不到助手面板规则"
    body = drawers[0]
    assert "position: static" in body or "position: sticky" in body, "助手应参与布局，不能 fixed 悬浮"
    assert "translateX" not in body, "助手不应再有滑入动画"


def test_ball_is_hidden():
    toggle = [b for b in _blocks(_css(), ".assistant-toggle") if "display" in b]
    assert toggle, "找不到悬浮球规则"
    assert any("display: none !important" in b for b in toggle), "悬浮球应彻底隐藏"


def test_no_overlay_reserve_rule_left():
    css = _css()
    assert "body.assistant-open .main" not in css, "浮窗时代的让位规则应已删除，播放器尺寸由栅格决定"


def test_toggle_js_still_self_heals_inline_offsets():
    js = _js()
    assert "pinToggleRight" in js, "缺少横向归位函数"
    assert 'removeProperty("left")' in js and 'removeProperty("right")' in js


def _html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


def test_language_core_is_the_only_switch():
    """语言只由 language.js 决定：index.html 先加载它，app.js 不再自己判 book.language 选音色。"""
    html = _html()
    assert html.index("/language.js") < html.index("/app.js"), "language.js 必须在 app.js 之前加载"
    lang = (WEB / "language.js").read_text(encoding="utf-8")
    for name in ("answerLanguage", "promptLanguage", "ttsVoice", "noteHeard", "setBook"):
        assert name in lang, f"language.js 缺少 {name}"
    js = _js()
    assert "Language.ttsVoice()" in js, "朗读音色应来自 language.js"
    assert "Language.noteHeard(" in js, "语音提问转写后应记录语言"


def test_upload_dialog_has_no_manual_language_controls():
    """导入弹窗只保留上传与生成按钮，语言自动识别——这些控件不得回归。"""
    html = _html()
    for gone in ("book-language", "generation-mode", "age-band", "upload-reviewer"):
        assert gone not in html, f"导入弹窗不应再有 {gone}"
    assert "lang-status" in html, "应有自动识别的语言说明"


def test_book_delete_control_is_wired():
    assert "book-delete" in _js() and "deleteBook(" in _js(), "书架条目缺少删除入口"
    assert ".book-delete" in _css(), "缺少删除按钮样式"
    assert "/api/books/" in _js() and '"DELETE"' in _js(), "删除应调用 DELETE /api/books/<id>"


def test_assets_are_cache_busted_and_icons_present():
    html = _html()
    for asset in ("/style.css?v=", "/app.js?v=", "/language.js?v="):
        assert asset in html, f"缺少缓存版本号：{asset}"
    assert 'rel="icon"' in html and "favicon.png" in html, "缺少站点图标"
    assert (WEB / "favicon.png").is_file(), "favicon.png 文件缺失"

def test_empty_state_keeps_paper_palette_without_hero():
    """空书架不放首屏，但底色仍是原来的米白纸感，不要纯白。"""
    html = _html()
    assert 'class="welcome"' not in html, "首屏欢迎区应已移除"
    assert "welcome-import" not in html and "welcome-settings" not in html, "首屏按钮不应存在"
    css = _css()
    assert ".app-shell.no-book { background: #fff" not in css, "空状态不要纯白底"
    assert "background-color: var(--paper)" in css, "页面底色应仍是 --paper 米白"
    assert "--paper:" in css, "缺少米白纸感变量"
    js = _js()
    assert "welcome-import" not in js and "welcome-settings" not in js, "首屏按钮接线应清理干净"
