# 数据字典 sitemapaudit

字段、单位、精度、可空与边界归属都在这里；页面只显示字段名和数字，不重复这些说明。

## 1 数值与编码

| 项 | 规则 |
|---|---|
| 计数 | 整数，`int`；页面千分位展示，导出原样 |
| 覆盖率 | Decimal，四位小数，`ROUND_HALF_UP`；`0.4706` 表示 47.06% |
| 时间戳 | 入库 UTC 秒级 `YYYY-MM-DDTHH:MM:SSZ`，展示转 Asia/Shanghai |
| `lastmod` | 按 sitemap 原文保留到秒，不补时区；比较用字符串序（ISO 8601 可直接比） |
| 状态码 | `int`，HTTP 原值；`0` 表示传输层失败（连接被拒/超时/越界主机） |
| 字节数 | `int`，响应体长度，不含响应头 |
| sha256 | 小写十六进制 64 位，输入资源与运行清单同源 |
| 事项编码 | 字符串，含前导零（`0070012`），禁止 `int()` |

## 2 地址与规范化

规范化 7 步，顺序固定（`app/modules/m2_url/normalize.py`）：

| 步 | 规则 | 产生痕迹 |
|---|---|---|
| scheme | 只接受 `http`/`https`；`javascript:`/`mailto:` 等进隔离区 | — |
| host | 主机小写、去末尾点、去默认端口（80/443） | `case`、`default_port` |
| relative | 相对地址按所在页面地址补全 | `relative` |
| path | 合并重复斜杠、去尾斜杠（根路径除外）；是否小写由站点档案决定 | `duplicate_slash`、`trailing_slash`、`case` |
| tracking | 丢弃跟踪参数与空值会话参数（清单见 `config.TRACKING_PARAMS`） | `tracking` |
| query | 旧档案按名称+值排序；新档案保留原始顺序（重复参数顺序有效） | `param_order`（仅旧档案） |
| fragment | 丢弃片段 | `fragment` |

### 2.1 地址规则档案（profile）

同一套步骤按站点选两个开关，历史批次随 run 记下当时的档案：

| 档案 | path 大小写 | query 顺序 | 用途 |
|---|---|---|---|
| `legacy`（默认） | 不区分（小写化） | 排序后视为同址 | 改版前口径、历史季度复算 |
| `strict` | 区分 | 原样保留，`?a=1&a=2` 与 `?a=2&a=1` 是两个地址 | 改版后口径 |

- 每个站点各有当前档案与版本号（`site_rules`）；切换记录在 `rule_switches`。
- 任何档案的规范化结果都带 `legacy_key`：按 `legacy` 档案算出的键。
  它是**对应依据**，不是新键的替代；新键始终保留原始大小写/参数顺序。
- 历史批次（`runs.payload.profiles` + `keys[].legacy_key`）按当时的档案复算，
  切换规则不会改写已入库数据。

### 2.2 键与判定

- **比较键** = 规范化后的完整地址（`scheme://host/path?query`）。
- 样例站物理上大小写不敏感（对标 IIS），所以 `legacy` 下大小写差异只算「地址不规范」，不算死链；
  改版后 `strict` 站点路径大小写区分页面。
- `relative` 与 `param_order` 是正常写法，不参与「地址不规范」判定；
  其余痕迹任一出现即判 `CANONICAL_MISMATCH`（`classify.MISMATCH_NOTES`）。
- 越界地址（主机不在站点清单）不进 key，记 `SCOPE_OFFSITE` / `LOC_OFFSITE`。

## 3 三向与分类

| 集合 | 定义 |
|---|---|
| 目录集 | 目录页 `data-role="catalog"` 区块内锚点可达的 key |
| 收录集 | 站点 `sitemap.xml` 的 loc 规范化后的 key |
| 可达集 | 对 key 发起逐跳探测的末态（`status`、`final_url`、`hops`） |

分类互斥，优先级由大到小：

| 分类 | 条件 | 严重度 |
|---|---|---|
| `CATALOG_DEAD` | 目录有该 key，末态 4xx/传输失败 | 错误 |
| `ONLY_IN_SITEMAP_DEAD` | 仅收录有，末态 4xx | 错误 |
| `REDIRECT_CHAIN` | 可达但跳转 ≥1 次 | 警告 |
| `DUPLICATE_SLUG` | 同侧多条原始写法折叠到同一 key | 警告 |
| `ONLY_IN_CATALOG` | 目录有、收录无 | 警告 |
| `ONLY_IN_SITEMAP_ALIVE` | 收录有、目录未登记 | 提示 |
| `CANONICAL_MISMATCH` | 双侧都有且 200 无跳转，但原始地址含不规范痕迹 | 提示 |
| `OK` | 双侧都有、200、无跳转、无痕迹 | — |

跳转上限 5 跳；超限或成环记 `PROBE_REDIRECT_LOOP`，状态码按 0 处理（死链）。

## 4 四账

| 账 | 等式 |
|---|---|
| 目录账 | `catalog_keys = both + only_in_catalog` |
| 收录账 | `sitemap_keys = both + only_in_sitemap` |
| 可达账 | `probed = alive_ok + alive_redirect + dead` |
| 缺陷账 | `findings_total = Σ 分类条数 = probed − 对平数` |

覆盖率 = 三向交集 / `catalog_keys`。四账任一不成立，整批状态记 `failed`。

## 5 空态三态

| 输入 | 结论码 | 是否算成功 |
|---|---|---|
| 响应体 0 字节 | `SITEMAP_EMPTY_BODY` | 否 |
| 只有 XML 声明，无 `urlset` | `SITEMAP_HEADER_ONLY` | 否 |
| 有 `urlset` 但 0 条 `url` | `EMPTY_SITEMAP` | 否 |
| 缺 `</urlset>` 或末块未闭合 | `SITEMAP_TRUNCATED` | 部分（已解析块保留） |
| 目录页 404/500 | `CATALOG_PAGE_FAILED` | 否 |

## 6 批次与幂等

| 字段 | 规则 |
|---|---|
| `input_digest` | 对 `url\tstatus\tsha256` 排序后取 sha256 |
| `batch_key` | `sha256(站点排序 + input_digest + 规则版本 + 按站档案)` |
| `run_id` | `ra-` + `batch_key` 前 12 位；同输入同档案必然同号 |
| `profiles` | 本批各站生效的地址档案，入库后不再随站点切换变化 |
| `profile_version` | 本批所用档案的最大版本号 |
| 复用 | 同 `batch_key` 且状态 `done` 时直接返回已入库结果，不新增行 |
| 阶段 | `crawl` / `probe` / `reconcile`，`stop_after` 可停在任一阶段 |
| 续跑 | 沿用该批入库时的 `profiles` 复算，不读切换后的当前档案 |

规则切换日志（`rule_switches`）：

| 字段 | 规则 |
|---|---|
| `switch_id` | `rs-` + 请求键前 10 位；同请求同号 |
| `request_key` | sha256(目标站点排序 + 目标档案 + 期望版本) |
| `status` | `applied` / `failed`；失败不改变站点档案 |
| 幂等 | 同 `request_key` 已有 `applied` 时直接返回，不再升版本 |

## 7 变更监测

快照 `schema = sitemapaudit/watch/1`，一条比较键一行（v1 可读，字段只增不改）：

| 字段 | 规则 |
|---|---|
| `key` | 本批档案下的比较键（`strict` 站点保留路径大小写） |
| `legacy_key` | 旧口径键（小写 + 参数排序），跨版本配对的依据 |
| `profile` | 该行所属站点的地址档案 |
| `status` | 探测末态，`0` 表示传输层失败 |
| `hop_count` | 跳转次数；`0` 表示未跳转 |
| `chain.kind` | `direct` / `chain` / `cycle` / `limit` |
| `raws` | 原始写法 + 补全基准（`base_kind` = `page` / `sitemap`） |
| `rows` | 同一比较键出现多行时保留较差的一条 |
| `profiles`（快照级） | 各站档案；v1 快照缺失时按 `legacy` 推断 |

三态：`status` 为 0 或 ≥400 记 `dead`；否则 `hop_count` > 0 记 `redirect`，其余 `ok`。

分桶（上次 × 本次，9 格固定表，另加独立桶）：

| 上次 | 本次 | 分桶 | 级别 |
|---|---|---|---|
| ok | ok | `unchanged` 未变化 | 提示 |
| ok | redirect | `new_redirect` 新恶化 | 警告 |
| ok | dead | `new_dead` 新增死链 | 错误 |
| redirect | ok | `recovered` 已修复 | 正常 |
| redirect | redirect | `still_redirect` 仍为跳转 | 警告 |
| redirect | dead | `new_dead` 新增死链 | 错误 |
| dead | ok | `recovered` 已修复 | 正常 |
| dead | redirect | `improved` 已改善 | 警告 |
| dead | dead | `still_dead` 仍未修复 | 错误 |
| — | 有 | `added` 新增地址 | 提示 |
| 有 | — | `removed` 消失地址 | 提示 |
| 两侧键不同、写法相同 | — | `basis` 基准不同 | 警告 |
| 档案切换、一拆多 | — | `resplit` 规则拆键 | 提示 |
| 档案切换、多合一 | — | `remerged` 规则并键 | 提示 |
| 无法唯一对应 | — | `unverified` 待核实 | 警告 |

### 7.1 跨版本配对依据（`match`）

| `match` | 条件 | 是否计入修复/新增 |
|---|---|---|
| `exact` | 两次比较键相同 | 是 |
| `legacy` | 键不同，但旧口径键相同且两侧各仅一条 | 是 |
| `raw` | 仍不同，同一原始写法在两侧各仅出现一次 | 是 |
| `resplit` | 旧口径一条、新口径多条，且能认出唯一延续 | 否，单列 |
| `remerged` | 旧口径多条、新口径一条，同上 | 否，单列 |
| `ambiguous` | 多对多，或拆/并组内认不出唯一延续 | 否，列待核实 |

- 只有 `exact`/`legacy`/`raw` 才能进入 `recovered`/`new_dead` 等归属。
- 档案切换时，另一侧完全没有对应键的行记 `unverified`，不再伪装成 `removed`/`added`。
- 回归清单 = `new_dead` + `new_redirect` + `still_dead` + `improved` + `basis` + `unverified`；
  `added` / `removed` / `resplit` / `remerged` 不进回归。
- 页面与 `/api/watch/regressions.csv` 调同一个 `service.watch`，口径一致；
  页面单表有展示上限，导出不受上限影响。
