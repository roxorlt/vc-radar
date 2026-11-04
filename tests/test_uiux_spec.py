"""UI/UX V2.0 规范与重写表现层验收测试（零外部依赖，离线可重复执行）。"""
import json
from pathlib import Path
import re

from vcradar import site, wording
from test_r14n_v17d_components import parse
from test_site_build import node, seeded, snapshot


SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "UIUX_SPEC.md"


def test_uiux_spec_document_and_three_layer_css_architecture():
    assert SPEC_PATH.is_file()
    spec = SPEC_PATH.read_text(encoding="utf-8")
    for heading in (
        "## 1. 产品定位与核心设计原则",
        "## 2. 信息架构与路由契约",
        "## 3. 设计令牌系统",
        "## 4. 三层 CSS 架构与核心组件规范",
        "## 5. 键盘交互、无障碍与安全文案红线",
    ):
        assert heading in spec

    tokens = (site.ASSETS / "tokens.css").read_text(encoding="utf-8")
    components = (site.ASSETS / "components.css").read_text(encoding="utf-8")
    style = (site.ASSETS / "style.css").read_text(encoding="utf-8")
    icons = (site.ASSETS / "icons.svg").read_text(encoding="utf-8")

    # 1. tokens.css 统一声明浅色、深色与无障碍减动效令牌，无重复碎片块
    assert tokens.count("\n:root {") + (1 if tokens.startswith(":root {") else 0) == 1
    assert ':root[data-theme="dark"]' in tokens
    assert "@media (prefers-color-scheme: dark)" in tokens
    assert "@media (prefers-reduced-motion: reduce)" in tokens
    for var in ("--c-paper", "--c-surface", "--c-ink", "--c-signal", "--t-display", "--t-read", "--s-2", "--r-lg", "--elev-1"):
        assert f"{var}:" in tokens

    # 2. components.css 已彻底移除历史废弃原型样式和竖线伪元素 hack
    assert len(components.splitlines()) < 350
    assert "\\2060｜" not in components + style
    assert 'content: "｜"' not in components + style
    for dead_cls in (".pblock", ".pfeat", ".shots", ".lb-stage", ".chat-day", ".bubble", ".cpop", ".statemenu", ".srcgroup", ".minibars"):
        assert dead_cls not in components + style

    # 3. style.css 不声明任何新变量或硬编码色值，所有资产无注释残留
    assert not re.search(r"--[\w-]+\s*:", style)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", style)
    for asset_text in (tokens, components, style, (site.ASSETS / "index.html").read_text(encoding="utf-8"), (site.ASSETS / "app.js").read_text(encoding="utf-8")):
        assert not re.search(r"//|/\*|\*/|<!--|-->", asset_text)

    # 4. 页面实际引用的所有图标在 icons.svg 中全部存在且无内部废弃备注
    assert "页面不用" not in icons
    defined_icons = set(re.findall(r'<symbol id="([^"]+)"', icons))
    used_icons = {f"i-{m}" for m in re.findall(r'icon\("([^"]+)"\)', (site.ASSETS / "app.js").read_text(encoding="utf-8"))}
    used_icons |= set(re.findall(r"#(i-[\w-]+)", (site.ASSETS / "index.html").read_text(encoding="utf-8")))
    for detail_icon in ("product", "problem", "business", "team", "funding", "competitor", "basis", "risk"):
        used_icons.add(f"i-{detail_icon}")
    assert used_icons <= defined_icons


def test_archive_active_filter_badge_and_reset_controls(seeded):
    path, _ = snapshot(seeded)
    result = node(
        r"""
        const fs = require("fs");
        const ui = require(process.argv[1]);
        const d = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
        const base = ui.listHtml(d, ui.defaultFilters());
        const active = ui.listHtml(d, {...ui.defaultFilters(), tag: d.wording.ai, round: d.wording.seed, backfill: true});
        console.log(JSON.stringify({
          baseCount: ui.activeFilterCount(ui.defaultFilters()),
          activeCount: ui.activeFilterCount({...ui.defaultFilters(), tag: d.wording.ai, round: d.wording.seed, backfill: true}),
          base,
          active
        }));
        """,
        path / "data.json",
    )
    assert result["baseCount"] == 0
    assert result["activeCount"] == 3
    base_tree = parse(result["base"])
    assert not base_tree.find("span", "n")
    assert not base_tree.find("button", "clear-f")

    active_tree = parse(result["active"])
    badges = active_tree.find("span", "n")
    assert len(badges) == 1 and badges[0].text == "3"
    inline_resets = active_tree.find("button", "clear-f")
    assert len(inline_resets) == 1 and inline_resets[0].text == "重置筛选"
    clear_buttons = [b for b in active_tree.find("button") if "data-clear-filters" in b.attrs]
    assert len(clear_buttons) == 2  # 移动工具栏快捷重置 + 桌面内联重置


def test_keyboard_j_k_card_navigation_theme_shortcut_and_styled_retry(seeded):
    path, _ = snapshot(seeded)
    result = node(
        r"""
        const fs = require("fs"), vm = require("vm");
        const listeners = {}, windowListeners = {}, storage = new Map();
        const links = [{id: "c1"}, {id: "c2"}, {id: "c3"}].map(item => ({
          ...item,
          scrolled: 0,
          focus() { doc.activeElement = this; },
          scrollIntoView() { this.scrolled++; }
        }));
        const root = {
          attrs: {},
          setAttribute(k, v) { this.attrs[k] = v; },
          removeAttribute(k) { delete this.attrs[k]; },
          getAttribute(k) { return this.attrs[k] ?? null; }
        };
        const main = {innerHTML: "", classList: {toggle() {}}, dataset: {loadFailed: "加载失败"}};
        const doc = {
          documentElement: root,
          body: {style: {}},
          activeElement: null,
          getElementById: id => id === "main" ? main : null,
          addEventListener: (k, fn) => { listeners[k] = fn; },
          querySelectorAll: selector => selector === ".co-card .co-link" ? links : []
        };
        const ctx = {
          URL,
          location: {hash: "#week"},
          document: doc,
          window: {addEventListener: (k, fn) => { windowListeners[k] = fn; }, scrollTo() {}},
          localStorage: {getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v)},
          matchMedia: () => ({matches: false}),
          fetch: async url => url === "./data.json"
            ? {ok: true, json: async () => JSON.parse(fs.readFileSync(process.argv[2], "utf8"))}
            : {ok: false, status: 500}
        };
        vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
        const tick = () => new Promise(resolve => setImmediate(resolve));
        (async () => {
          await tick(); await tick();
          listeners.keydown({key: "j", preventDefault() {}});
          const first = doc.activeElement?.id;
          listeners.keydown({key: "j", preventDefault() {}});
          const second = doc.activeElement?.id;
          listeners.keydown({key: "k", preventDefault() {}});
          const backToFirst = doc.activeElement?.id;
          listeners.keydown({key: "t", preventDefault() {}});
          const toggledTheme = root.attrs["data-theme"];

          ctx.location.hash = "#c/mach9";
          windowListeners.hashchange();
          await tick(); await tick();
          const retryHtml = main.innerHTML;
          console.log(JSON.stringify({first, second, backToFirst, toggledTheme, retryHtml}));
        })().catch(err => { console.error(err); process.exitCode = 1; });
        """,
        path / "data.json",
    )
    assert result["first"] == "c1"
    assert result["second"] == "c2"
    assert result["backToFirst"] == "c1"
    assert result["toggledTheme"] == "dark"
    assert '<button type="button" class="btn" id="retry-detail">' in result["retryHtml"]
    assert wording.SITE_TEXT["detail_failed"] in result["retryHtml"]
