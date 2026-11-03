import os
"""使用已有 Chromium 离线检查移动端布局和浏览器交互，不安装任何依赖。"""
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess

import pytest

from test_site_build import seeded, snapshot


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


@pytest.mark.parametrize("route", ["#week", "#list", "#c/mach9", "#sources"])
def test_390px_layout_routes_and_interactions(seeded, route, tmp_path):
    if os.environ.get("VCRADAR_BROWSER_TESTS") != "1":
        pytest.skip("浏览器布局检查默认关闭；设 VCRADAR_BROWSER_TESTS=1 开启（无头 Chromium 在本机 --dump-dom 会挂起）")
    browser = Path("/opt/ungoogled-chromium/chrome")
    if not browser.exists():
        pytest.skip("未安装用于离线布局验证的 Chromium")
    path, _ = snapshot(seeded)
    # 长单词和超长来源地址也不能把页面撑宽。
    conn, _ = seeded
    conn.execute("UPDATE company SET summary=? WHERE id='flapping'", ("Software" * 70,))
    conn.execute("UPDATE event_source SET url=? WHERE event_id=2", ("https://techcrunch.com/" + "long-path" * 70,))
    snapshot(seeded)
    (path / "verify.js").write_text("""
      (async () => {
        for (let i=0;i<100 && document.getElementById('main').textContent.includes('正在读取');i++)
          await new Promise(resolve=>setTimeout(resolve,10));
        const errors=[];
        const checkWidth=()=>{if(document.documentElement.scrollWidth>innerWidth)errors.push('横向溢出');};
        checkWidth();
        let interactions=true;
        if(location.hash==='#list') {
          document.querySelector('[data-filter="period"][data-value="week"]').click();
          interactions=!!document.querySelector('[data-value="week"].on') && !document.getElementById('main').textContent.includes('TryNearby');
          document.querySelector('[data-filter="period"][data-value="all"]').click();
          interactions=interactions && document.getElementById('main').textContent.includes('TryNearby');
          const select=document.getElementById('issue'); select.value='date:2026-10-23';
          select.dispatchEvent(new Event('change',{bubbles:true}));
          interactions=interactions && document.getElementById('main').textContent.includes('Terse') && !document.getElementById('main').textContent.includes('TryNearby');
        }
        if(location.hash==='#sources') {
          document.querySelectorAll('details').forEach(d=>d.open=true); checkWidth();
        }
        if(location.hash==='#week') {
          document.querySelector('details').open=true; checkWidth();
        }
        document.documentElement.setAttribute('data-check',JSON.stringify({width:innerWidth,errors,interactions,
          text:document.getElementById('main').textContent,
          external:[...document.querySelectorAll('a[target="_blank"]')].every(a=>a.rel==='noopener nofollow'),
          resources:performance.getEntriesByType('resource').map(r=>r.name)}));
      })().catch(e=>document.documentElement.setAttribute('data-check',JSON.stringify({error:String(e)})));
    """, encoding="utf-8")
    index = path / "index.html"
    index.write_text(index.read_text().replace("</body>", '<script src="./verify.js" defer></script></body>'))
    result = subprocess.run([str(browser), "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
                             "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
                             "--host-resolver-rules=MAP * ~NOTFOUND", "--allow-file-access-from-files", "--hide-scrollbars",
                             "--window-size=390,844", "--force-device-scale-factor=1", "--virtual-time-budget=3000",
                             f"--user-data-dir={tmp_path / 'browser-profile'}", "--dump-dom", index.as_uri() + route],
                            text=True, capture_output=True, timeout=25)
    if result.returncode and "Operation not permitted" in result.stderr:
        pytest.skip("当前沙箱拒绝 Chromium 启动，未能执行 390 像素浏览器检查")
    assert result.returncode == 0, result.stderr[-1000:]
    checked = BrowserResult(result.stdout).result
    assert checked is not None, result.stderr[-1000:]
    assert "error" not in checked, checked
    assert checked["width"] == 390
    assert checked["errors"] == [] and checked["interactions"] and checked["external"]
    assert "暂时无法读取" not in checked["text"] and "正在读取" not in checked["text"]
    assert all(url.startswith(path.as_uri()) for url in checked["resources"])
    if route == "#c/mach9":
        assert "产品是什么" in checked["text"] and "有新证据：总部" in checked["text"]
