import os
"""使用已有 Chromium/Chrome 离线检查移动端（390px）与桌面端（1280px）布局和浏览器交互，不安装任何依赖。"""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from test_site_build import seeded, snapshot


def _find_browser() -> Path | None:
    candidates = [
        os.environ.get("VCRADAR_CHROME_BIN"),
        "/opt/ungoogled-chromium/chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


class BrowserResult(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.result = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "html":
            value = dict(attrs).get("data-check")
            if value:
                self.result = json.loads(value)


def _run_browser_check(seeded, route: str, width: int, height: int, tmp_path: Path):
    if os.environ.get("VCRADAR_BROWSER_TESTS") != "1":
        pytest.skip("浏览器布局检查默认关闭；设 VCRADAR_BROWSER_TESTS=1 开启")
    browser = _find_browser()
    if browser is None:
        pytest.skip("未安装用于离线布局验证的 Chromium / Google Chrome")
    path, _ = snapshot(seeded)
    # 长单词和超长来源地址也不能把页面撑宽。
    conn, _ = seeded
    conn.execute("UPDATE company SET summary=? WHERE id='flapping'", ("Software" * 70,))
    conn.execute("UPDATE event_source SET url=? WHERE event_id=2", ("https://techcrunch.com/" + "long-path" * 70,))
    snapshot(seeded)

    # 通过同目录精确尺寸 iframe 承载被测页面，规避 macOS Chrome 顶层窗口最小宽度 500px 限制，同时不篡改 index.html。
    host = path / f"_vp_host_{width}.html"
    target_uri = f"./index.html{route}"
    host.write_text(f"""<!doctype html><html><body style="margin:0;padding:0;overflow:hidden">
      <iframe id="vp" src="{target_uri}" style="width:{width}px;height:{height}px;border:0;display:block"></iframe>
      <script>
      (async () => {{
        const frame = document.getElementById('vp');
        for (let i=0;i<200 && (!frame.contentDocument || !frame.contentDocument.getElementById('main') || frame.contentDocument.getElementById('main').textContent.includes('正在读取'));i++)
          await new Promise(resolve=>setTimeout(resolve,10));
        const win = frame.contentWindow, doc = frame.contentDocument;
        const errors=[];
        const checkWidth=()=>{{if(doc.documentElement.scrollWidth>win.innerWidth)errors.push('横向溢出:'+doc.documentElement.scrollWidth+'>'+win.innerWidth);}};
        checkWidth();
        let interactions=true;
        if(win.location.hash==='#list') {{
          if(win.innerWidth < 1024) doc.querySelector('#open-filters').click();
          doc.querySelector('[data-filter="period"][data-value="week"]').click();
          await new Promise(resolve=>setTimeout(resolve,20));
          interactions=!!doc.querySelector('[data-value="week"].on') && !doc.getElementById('main').textContent.includes('TryNearby');
          doc.querySelector('[data-filter="period"][data-value="all"]').click();
          await new Promise(resolve=>setTimeout(resolve,20));
          interactions=interactions && doc.getElementById('main').textContent.includes('TryNearby');
          doc.querySelector('[data-filter="period"][data-value="issue"]').click();
          await new Promise(resolve=>setTimeout(resolve,20));
          const select=doc.getElementById('issue'); select.value='date:2026-10-23';
          select.dispatchEvent(new win.Event('change',{{bubbles:true}}));
          await new Promise(resolve=>setTimeout(resolve,20));
          interactions=interactions && doc.getElementById('main').textContent.includes('Terse') && !doc.getElementById('main').textContent.includes('TryNearby');
          if(win.innerWidth < 1024) doc.querySelector('[data-close-filters]').click();
          checkWidth();
        }}
        if(win.location.hash==='#sources') {{
          doc.querySelectorAll('details').forEach(d=>d.open=true); checkWidth();
        }}
        if(win.location.hash==='#week') {{
          doc.querySelectorAll('details').forEach(d=>d.open=true);
          doc.dispatchEvent(new win.KeyboardEvent('keydown',{{key:'j',bubbles:true}}));
          interactions = interactions && !!doc.activeElement && doc.activeElement.classList.contains('co-link');
          checkWidth();
        }}
        document.documentElement.setAttribute('data-check',JSON.stringify({{width:win.innerWidth,errors,interactions,
          text:doc.getElementById('main').textContent,
          external:[...doc.querySelectorAll('a[target="_blank"]')].every(a=>a.rel==='noopener nofollow'),
          resources:win.performance.getEntriesByType('resource').map(r=>r.name)}}));
      }})().catch(e=>document.documentElement.setAttribute('data-check',JSON.stringify({{error:String(e)}})));
      </script></body></html>""", encoding="utf-8")

    try:
        result = subprocess.run([str(browser), "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
                                 "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
                                 "--host-resolver-rules=MAP * ~NOTFOUND", "--allow-file-access-from-files", "--hide-scrollbars",
                                 f"--window-size={max(width, 520)},{max(height, 900)}", "--force-device-scale-factor=1", "--virtual-time-budget=3500",
                                 f"--user-data-dir={tmp_path / f'browser-profile-{width}'}", "--dump-dom", host.as_uri()],
                                text=True, capture_output=True, timeout=25)
    finally:
        host.unlink(missing_ok=True)
    if result.returncode and "Operation not permitted" in result.stderr:
        pytest.skip("当前沙箱拒绝 Chromium 启动，未能执行浏览器检查")
    assert result.returncode == 0, result.stderr[-1000:]
    checked = BrowserResult(result.stdout).result
    assert checked is not None, result.stderr[-1000:]
    assert "error" not in checked, checked
    assert checked["width"] == width
    assert checked["errors"] == [] and checked["interactions"] and checked["external"]
    assert "暂时无法读取" not in checked["text"] and "正在读取" not in checked["text"]
    assert all(url.startswith(path.as_uri()) for url in checked["resources"])
    if route == "#c/mach9":
        assert "产品是什么" in checked["text"] and "资料有出入：国家" in checked["text"]


@pytest.mark.parametrize("route", ["#week", "#list", "#c/mach9", "#sources"])
def test_390px_layout_routes_and_interactions(seeded, route, tmp_path):
    _run_browser_check(seeded, route, 390, 844, tmp_path)


@pytest.mark.parametrize("route", ["#week", "#list", "#c/mach9"])
def test_1280px_desktop_layout_routes_and_interactions(seeded, route, tmp_path):
    _run_browser_check(seeded, route, 1280, 900, tmp_path)
