# vc-radar

早期创投公开信息监控与每日简报。

vc-radar 定时抓取公开的创投新闻、RSS、公司目录和融资公告，抽取「哪家公司、在哪、融了多少、哪一轮、谁投的」，去重合并后按推送日生成一份中文简报文件，并可生成一个静态的回看页面（本周、往期、按来源和标签筛选）。

只使用公开可访问的页面和接口，遵守各站点的 robots.txt；需要登录或付费的数据不抓。

## 目录结构

```
vcradar/              主程序（纯 Python 标准库）
  cli.py              命令行入口：python -m vcradar
  config.py           配置默认值、配置文件和命令行覆盖
  sources/            各信源的注册表和解析器（RSS、网页、接口）
  fetcher.py http.py robots.py   逐地址抓取、超时与出错判定、robots 判定
  extract.py llm.py prompts/     规则抽取 + 模型抽取（提示词在 prompts/）
  scan.py db.py models.py        入库、去重合并、轮次和国家归类、SQLite 数据层
  verify.py homepage.py deep.py  入库查证、官网核对、深度补充资料
  fx.py placenames.py wording.py 汇率换算、地名中文写法、用词规范
  render.py outbox.py            简报文字和交接文件
  site.py site_assets/           静态回看页的生成和前端资源
config/               公开的规则配置：信源分级、国家/城市/币种表、官网核对排除名单等
  vc-radar.toml.example  运行配置示例
tests/                离线测试；fixtures/ 下全部是手写的测试数据
scripts/              辅助脚本（如统计回看页数据大小）
```

## 本地运行

需要 Python 3.13（运行时只用标准库）。

```bash
python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt     # 只装测试用的 pytest
```

运行数据（SQLite 库、原始响应缓存、简报文件、回看页）放在仓库外的数据目录，默认 `/srv/vc-radar/state`。复制 `config/vc-radar.toml.example` 为数据目录下的 `config.toml` 后按需修改，或用环境变量 `VC_RADAR_CONFIG` 指定配置文件路径。

模型抽取使用 DeepSeek 接口，密钥只从环境变量 `DEEPSEEK_API_KEY` 或配置里 `env_file` 指向的文件读取，不要写进仓库（`.gitignore` 已排除 `.env` 和数据库文件）。

常用命令：

```bash
python -m vcradar --test --dry-run          # 测试运行：写测试库和 outbox-test，不影响正式数据
python -m vcradar --test --fixture tests/fixtures/sources/manifest.json --dry-run   # 用本地固定返回离线跑一遍
python -m vcradar --build-site              # 只读数据库，生成回看页
python -m vcradar --preview-push            # 预览推送文字
python -m vcradar --help                    # 全部参数
```

## 测试

```bash
python -m pytest -q
```

测试全部离线，不访问网络、不需要密钥；网络和模型调用都用 `tests/fixtures/` 下的手写数据替代。少数浏览器布局用例默认跳过。

## 信源配置

- 信源清单在 `vcradar/sources/registry.py`：每个信源一行，写明编号、名称、抓取地址、类型（rss / api / page）、过期天数和抓取顺序；解析器在 `vcradar/sources/` 下按信源分文件。
- 新增信源：在注册表加一行，写对应解析器，并在 `tests/fixtures/` 放手写的固定返回、在 `tests/` 加解析测试。
- `config/source-tiers.json`：来源分级（官方、通讯社、媒体、其他），用于查证时判断证据强弱。
- `config/not-connected.json`：评估过但暂不接入的来源及原因（例如 robots 禁止、需要登录）。
- `config/homepage-check.json`、`config/public-suffixes.json`：官网核对时排除的域名和公共后缀。
- `config/country-names.json`、`config/city-country.json`、`config/city-names-zh.json`、`config/currencies.json`：国家、城市和币种的中文写法与对应关系。
- 抽取提示词在 `vcradar/prompts/`。

## 许可

代码以仓库所有者的名义公开。`vcradar/site_assets/fonts/` 下的 Inter 字体按 SIL Open Font License 1.1 分发，见同目录 `LICENSE.txt`。
