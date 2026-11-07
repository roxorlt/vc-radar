# Social Radar（社交/社群产品发现与深度拆解）架构设计

> 日期：2026-11-07  
> 分支：`feat/social-product-radar`

## 1. 目标与边界

将 `vc-radar` 升级为**以具体社交/社群产品为主角**的产品发现与多方交叉拆解站（Social Radar）：

1. **主角切换为具体产品**：每张卡片与详情浮层围绕可体验或可讨论的具体 App / Web 社交产品展开，展示产品图标、界面/实拍配图、一句话切入点、6 维度大白话深度拆解，并关联所属公司、融资轮次/金额、投资方与多国榜单表现。
2. **品类边界清晰**：聚焦人与人连接型社交与社区产品（熟人/密友、陌生人/破冰、兴趣/品味社区、多人共创、校园/破冰、AI辅助连接），剔除纯单机工具、纯 AI 虚拟伴侣/陪练机器人与企业级 B2B SaaS；非社交类 AI 融资继续保留在 SQLite（`ai_label='是'`）供后续下游项目消费，本次不改动其他仓库。
3. **标准化多源取材与交叉比对（非单品特判）**：每一个候选产品统一走标准离线流水线——拉取多国商店元数据与真实高赞/差评、抓取官网、调用搜索服务检索热门评测帖/创始人专访/运营数据披露并提取正文实拍图，最后做多方交叉比对并预生成中文母语大白话拆解，前端点击 0ms 秒开。
4. **Cloudflare R2 媒体托管**：所有产品图标、商店截图与热帖实拍图按内容哈希（`SHA-256`）去重上传至 Cloudflare R2 存储桶 `vc-radar-media`，由同源 Pages Function `/media/[[key]].js` 带强缓存响应头直出，杜绝外链防盗链失效与仓库体积膨胀。

---

## 2. 四层发现漏斗（A + B 结合）

| 层级 | 来源 | 覆盖盲区与作用 |
| :--- | :--- | :--- |
| **A1 多国双榜新品** | Apple App Store 官方 RSS（`US / JP / KR / TW / HK / GB` × `6005 社交 / 6008 摄影录像 / 6012 生活` Top Free）+ `itunes.apple.com/lookup` 批量查验近 18 个月新上架包 | 捕捉像 `setlog`、`Instants` 这样未发欧美融资通稿、或挂在摄影分类下但在日韩港台冲榜的新锐社交产品，合并记录跨区最高排名 |
| **A2 早期产品社区** | Product Hunt 最新产品 Feed + Hacker News `Show HN`（Algolia 公开检索社交/社区新品） | 捕捉刚发布 Web / TestFlight / iOS 版本的独立社交新品 |
| **B 资本早期（融资 / YC）** | 既有 18 个 VC / YC 早期来源中 `social_label='是'` 或命中消费社交特征的公司 + 官网存活与商店上架探测 | 区分「已上架可体验（iOS / Android / Web）」与「内测排队 / 暂未开放」，供前端筛选切换 |

---

## 3. 标准化单品富化、搜索热帖与交叉比对流水线

每个候选产品统一执行以下 5 步离线处理（`vcradar/product_pipeline.py`）：

1. **商店与官网探测**：
   - 若来自 VC/社区且未知 `app_store_id`，在遵守 `robots.txt` 前提下抓取官网，提取 `apps.apple.com` / `play.google.com` 链接、OpenGraph 主图与内测排队信号（`waitlist` / `early access`），回查 iTunes Lookup 确认真实上架状态。
   - 若有 App Store ID，拉取多区 Lookup 元数据（描述、更新日志、评分、开发者）及官方 `customerreviews/id={id}/sortBy=mostHelpful/json` 真实用户评论（兼顾 4–5 星好评高频爽点与 1–2 星差评真实槽点）。
2. **搜索服务检索热门帖子与报道**：
   - 调用 `SearchClient`（`POST https://api.deepseek.com/v1/tools/search`，开启 `need_content=True`）及公开社区讨论接口（Hacker News / Reddit JSON），对每个产品发出标准化多语种检索式：
     - `"{name}" "{developer_or_domain}" app interview OR DAU OR downloads OR funding`
     - `"{name}" app review OR features OR how it works`
     - `"{name}" App 评测 OR 玩法 OR 拆解`
   - 相关性过滤去重后，对排名前列的公开评测/专访网页提取清洗正文与正文内高分辨率配图（过滤头像、角标、追踪像素及 `< 18KB` 小图）。
3. **Cloudflare R2 内容寻址图片镜像（`vcradar/r2.py`）**：
   - 下载图标、App Store 截图（至多 4 张）与热帖/官网实拍图（至多 3 张），校验真实 MIME 魔数（JPEG / PNG / WebP），计算 `SHA-256` 写入 `media_asset` 表并幂等上传至 `vc-radar-media` 存储桶，统一输出同源路径 `/media/<sha256>.<ext>`，标注图片来源类型（`报道实拍图` / `App Store 截图` / `官网实景图`）。
4. **多方交叉比对与 22 岁产品爱好者大白话预生成**：
   - 将「官方商店/官网介绍 + 媒体/创始人专访数据 + App Store/社区真实正负向反馈」一并送入 `deepseek-flash`，要求模型先做社交品类准入判定与交叉比对（区分多方印证事实、单方披露数据与官方宣传 vs 真实用户反馈的反差），再输出 6 个固定维度的中文母语大白话拆解：
     1. `产品形态与切入点`
     2. `目标人群与场景`
     3. `核心社交机制与关系链`
     4. `冷启动与增长飞轮`
     5. `真实口碑、野生玩法与踩过的坑`
     6. `商业化与可参考讨论点`
   - 后置黑名单词表校验（拦截「赋能、抓手、闭环、心智、场域、沉淀、矩阵、组合拳」等互联网黑话与违规旧词）。
5. **静态快照内联构建（0ms 秒开）**：
   - `site.build_site()` 将已准入且预生成完毕的 `social_product` 列表内联写入 `site/data.json` 的 `products` 字段（总大小严格受 `500KB` 预算与发布脱敏守卫检查约束），前端点击任意卡片直接从内存渲染完整档案，彻底消除「解读生成中」等待态。

---

## 4. 数据表设计（SQLite 幂等增量表）

在不破坏既有 12 张业务表与 3,300+ 历史回归测试的前提下，于 `vcradar/db.py` 增量维护 3 张表：

- `social_product`：存储产品核心标识、发现通道（`vc` / `chart`）、上架状态（`ios` / `android` / `web` / `waitlist`）、出品背景（`indie` / `bigtech`）、多区榜单排名、机制标签、一句话钩子、6 维度拆解 JSON、交叉比对摘要 JSON、R2 媒体列表 JSON 及外部参考热帖链接 JSON。
- `product_evidence`：按 `(product_id, source_kind, url)` 存储商店元数据、真实评论摘录、官网正文及搜索命中的热门帖子正文摘录与配图来源，保证每条结论可追溯。
- `media_asset`：按 `src_url` 与 `sha256` 缓存已上传至 Cloudflare R2 `vc-radar-media` 的媒体文件元数据，避免重复下载与重复上传。
