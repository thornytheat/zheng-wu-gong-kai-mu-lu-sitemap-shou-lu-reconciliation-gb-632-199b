# sitemapaudit 政务公开目录与 sitemap 收录对账

河州市政务公开办网站编辑部每季度要核对 3 个政务站的公开目录与 sitemap 收录：
目录页写了什么、sitemap 收录了什么、这些地址还能不能打开。现状是打开三个站
逐条肉眼比对，2.4 万条目录对不完。本项目把这段流程做成一次可复算的巡检，
并把本次结果与上季度对比，直接给出一份回归清单。

## 能力

- 采信：三站目录页 + `sitemap.xml`，编码回退链、BOM 剥离、块级严格校验、行号保真
- 规范化：7 步固定顺序得到比较键，改写痕迹逐条留证；按站点选用地址规则档案
- 规则档案：旧口径（路径不分大小写、参数排序）/ 新口径（路径区分大小写、重复参数顺序有效），
  按站点生效，切换前回放历史校验，失败保持旧规则，重试不重复生效
- 可达性：逐跳探测（不自动跟随），状态码 0/4xx/5xx 分开记，跳转上限 5 跳
- 三向对账：目录 / 收录 / 可达 三集合，7 类缺陷 + 对平，四账守恒
- 缺陷单：按分类筛选，CSV（含原始写法）与运行清单 JSON 导出，可打印
- 批次：批次键幂等重跑、阶段可单独跑、可续跑
- 变更监测：与上次巡检比新增死链 / 已修复 / 新恶化，按站点看趋势，导回归清单与快照
- 跨版本比较：历史批次按原档案复算，新增/消失/修复都给出对应依据，无法唯一对应的列「待核实」

## 模块

| 模块 | 动作 | 入口 |
|---|---|---|
| M1 目录采信 | 取目录页与 sitemap，逐行留行号，坏块进隔离区 | `POST /api/crawl` |
| M2 URL 规则 | 契约查询、按站规则切换、地址规范化试算 | `GET /api/contract` `GET /api/rules` `POST /api/rules/switch` `POST /api/url/normalize` |
| M3 可达性 | 逐地址状态码、跳转链、分类 | `GET /api/reachability` |
| M4 三向对账 | 四账、交叉表、分类合计 | `GET /api/reconcile` |
| M5 巡检批次 | 批次历史、停止点、续跑 | `GET /api/batches` `POST /api/batches/{run_id}/resume` |
| M6 缺陷单 | 缺陷筛选、CSV、运行清单 | `GET /api/defects` `GET /api/defects.csv` `GET /api/export/manifest.json` |
| M7 变更监测 | 两次巡检对比、站点趋势、回归清单、快照导出 | `GET /api/watch` `GET /api/watch/regressions.csv` `GET /api/watch/snapshot.json` |

页面：`/` 巡检运行、`/sites` 站点与目录、`/reach` 可达性明细、`/reconcile` 对账缺陷、
`/batches` 批次历史、`/defects` 缺陷单、`/watch` 变更监测、`/rules` 地址规则。

## 核心规则

1. 规范化 7 步（scheme → host → relative → path → tracking → query → fragment），
   比较键 = 规范化后的完整地址；规则表见 `docs/DATA-DICTIONARY.md`。
   其中 path 与 query 两步的开关由站点档案决定：
   - `legacy`（默认）：路径整体小写、参数按名称+值排序，与上季度口径一致；
   - `strict`：路径保留原始大小写、参数保留原始顺序（重复参数顺序有效）。
   任何档案的结果都另带 `legacy_key`（旧口径键），历史复算与跨版本配对用它，绝不覆盖新键。
2. 分类互斥，优先级固定：死链 > 跳转 > 重复条目 > 漏收录 > 目录未登记 > 地址不规范 > 对平。
   规则表带优先级排序后判定，书写顺序不影响结论。
3. 四账：目录 / 收录 / 可达 / 缺陷四个等式必须成立，否则整批记 `failed`。
4. 变更对比按同一把尺子配对：同站点档案未变时逐键比较；档案变了先按旧键找同一地址，
   再按原始写法唯一对应。拆键、并键单列，无法唯一对应的列「待核实」，都不计入修复或新增。
5. 只在单侧出现的地址先看补全基准：同一相对写法按目录页地址与 sitemap 地址补全出
   两个键时记「基准不同」，不算新增也不算消失。

## 规则切换

- 每个站点各有一份地址档案与版本号，历史批次把当时的档案随 run 入库。
- 切换顺序固定：用候选档案回放最近几批原始行 → 校验键幂等、历史可复现 → 更新站点档案并记日志。
  校验不过不写库，旧档案原样保留（等于回滚），接口返回 `E_RULE_SWITCH_FAILED`。
- 同一个切换请求（站点 + 档案 + 期望版本）重试命中已有成功记录，不重复生效、不再升版本；
  期望版本过期返回 `E_RULE_VERSION`。
- 批次键包含按站档案：换了规则就是新批次，不会误命中旧结果；续跑沿用入库时的档案。

## 规则与已知不覆盖

- 数值与时间规则、空态三态、批次键定义见 `docs/DATA-DICTIONARY.md`。
- 陷阱登记与不覆盖项见 `docs/DEPTH.md`（含 12 条 TRAP 与对应测试）。
- 变更对比只看三态与跳转链，不比 `lastmod`、标题与正文。
- 基准来源：库内更早批次优先，其次 `sample_data/watch/baseline.json`；都没有时页面提示。
- 已知不覆盖：`sitemapindex` 索引、正文内容比对、并发探测。

## 启动

```bash
docker compose up -d --build          # 主服务 18201，样例站 19201
curl -s localhost:18201/healthz
curl -s -X POST localhost:18201/api/crawl -H 'content-type: application/json' -d '{}'
curl -s localhost:18201/api/watch
```

本地不装容器时：

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
SAMPLE_SITE_BASE=http://127.0.0.1:19201 python3 -m http.server 19201 --directory sample_site &
SAMPLE_SITE_BASE=http://127.0.0.1:19201 SITEMAPAUDIT_DB=/tmp/audit.sqlite \
  uvicorn app.main:create_app --factory --port 18201
```

重建数据库：删除 `audit-data` 卷（`docker compose down -v`）后重新启动。

## 演示步骤

1. 打开 `http://localhost:18201/`，点「触发巡检」，看三阶段账与原因码分布。
2. `/sites` 看目录条目：`site-b` 的编码是 `gb18030`，`site-a` 有旧属性与冲突行。
3. `/reach` 按分类过滤，点开 `跳转链` 能看到两跳的旧路径。
4. `/reconcile` 看四账与三向交叉表；`/defects` 导出 CSV 与运行清单并打印。
5. `/watch` 看与上季度快照的差异，导出回归清单与本次快照。

## 测试

```bash
pytest -q          # 127 个测试函数 / 129 个用例
```

## 目录

```
app/                     平台层 + 七个功能模块
app/templates/           9 个页面（base + 8）
app/static/              theme.css layout.css components.css + 4 个脚本
docs/                    DEPTH.md DATA-DICTIONARY.md
sample_site/             三站目录页 + sitemap + serve.py（样例站服务）
sample_data/degenerate/  空态与截断样例
sample_data/watch/       上一季度快照（变更对比的样例基准）
tests/                   每模块一组 + e2e + fixtures
```
