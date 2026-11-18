# robotscope — 全站发现：sitemap/robots、host 礼貌限速、抓取预算与调度

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx`（可选）+ `pytest`／`unittest`（离线，无需联网）。

## 运行方式

```text
make run     # 旧记录流水线：读 fixtures/input.json → {fed, rows}
make plan    # 离线演练：fixtures/crawl.json + fixtures/offline_routes.json → 覆盖/节奏/额度摘要
make crawl   # 离线演练：规划 + 派发 + 抓取 + 回执，再输出报告
make test    # 有 pytest 用 pytest，否则用 unittest；全部离线
```

CLI 两种入口互不干扰：

```text
python -m robotscope.cli                       # 旧流水线（行为不变）
python -m robotscope.cli --plan --input ...    # 爬取规划报告（JSON）
python -m robotscope.cli --run  --input ... --offline ...   # 执行一批
  --format summary   # 人类可读摘要
  --db PATH          # 规划状态 SQLite（默认 ./robotscope.db）
```

没有 `--offline` 映射时规划器不联网：robots 取不到就**等待**，不会默认放行。

## 模块边界（单一职责）

| 模块 | 职责 |
| --- | --- |
| `sitemap` | 递归 sitemap/index 解析、合并、缓存、完整性 |
| `robots` | robots.txt 解析、按主机/路径/UA 的带版本判定、缓存三种失败态 |
| `policy_engine` | 规则集版本化与准入判定（robots 拒绝绝对优先） |
| `value_rank` | 历史产出/成本/截止时间/防饥饿排序 |
| `budget` | 每日额度、预留、回执、退款、迟到回执 |
| `politeness` | 主机节奏（Crawl-delay）、主机等待、队列 |
| `lease` | 并发占位与带 fence 的租约 |
| `planner` | 唯一协调点：发现→准入→排序→额度→主机→占位 |
| `compat` / `fetch` | 旧输入兼容、离线/HTTP 抓取器 |
| `state` / `clock` | SQLite 状态、单调逻辑时钟 |

`pipeline.py` 仍是旧流水线的薄编排；新流程的业务判断全部在各业务模块内，`planner.py` 只按顺序调用。

## 关键语义

**索引发现**：sitemap index 递归遍历；环（A→B→A）与共享引用（两个索引指向同一子图）分别记录在 `cycle_edges` / `shared_edges`，只访问一次。某个子 sitemap 失败不会中断整体扫描，但结果标记 `complete=False`；**未完成的扫描不能推断删除**，`deletion_candidates()` 与报告里的 `deletion_inference=blocked_incomplete_scan` 都体现在这一点上。

**规则准入**：判定顺序是 URL 形状 → 站点/路径规则 → robots → 默认拒绝。robots 证据按 `(host, path, user_agent)` 绑定并带内容版本 `r1:<digest>`；证据的主机、路径或 UA 不匹配即视为无证据。缓存**缺失**、**过期**、**获取失败**是三种不同结果：

- 缺失 → `cache_missing`，等待；
- 过期但可刷新 → 刷新后按新版本判定；刷新失败 → `expired_refresh_failed`，等待；
- 过期未刷新 → 允许类 `stale_unverified` 不放行；拒绝类 `stale_disallowed` 仍拒绝；
- HTTP 404/410 → 有版本的 `robots_absent`（允许）；
- 其他 HTTP 状态/网络异常 → `fetch_failed`，带退避，不视为许可。

高优先级、额度、截止时间都不能把 `disallowed` 变成允许。

**价值排序**：`score = 历史产出(对数阻尼) + 成本倒数 + 截止紧迫度 + 等待时间/防饥饿 + 站点优先级`；每轮按 `round_size` 轮转最久未被服务的主机，避免大站饿死小站。所有分量在 `breakdown` 里可解释。

**额度、等待、占位是三本账**：

- 额度按 UTC 日计，`request_id` 幂等，重复申请不重复占用；
- 主机等待（429/503、Retry-After、robots 退避）期间请求在 `waiting_host`，**不占并发位**；
- 并发位按全局/单主机计（`waiting_slot`），派发顺序是 准入 → 主机 → 额度 → 占位；占位失败会把额度退回。

**租约与旧 worker**：每次抢占发新 `fence`；租约回收后旧 `fence` 的 `validate/may_execute` 返回 False，旧 worker 不得执行。`complete` 仍接受迟到回执（结果标记 `lease_stale`），但**已派发的请求不退款**。

**规则更新**：`apply_policy()` 使未执行项失效，释放其额度预留与队列/占位；已执行、在途、结果未知的请求列在 `not_refunded`，不退款。

**时间与重启**：`Clock` 保存逻辑高水位并落库，时钟回拨不会重开额度日或缩短等待。跨日迟到回执按**实际执行日**入账（`execution_day`），回执日仅记为 `late`。所有状态（规则版本、计划、账本、租约、robots 缓存）都在 SQLite，进程重启后一致。

**保守等待 vs 进度**：缺少可用 robots 证据时宁可等待也不抓取；每个延后原因（`waiting_host` / `waiting_budget` / `waiting_slot` / `unassessed`）都出现在 `plan["deferrals"]` 与报告中，`plan["tradeoff"]` 显式说明该取舍。

**兼容旧输入**：`compat.parse_input()` 接受旧的扁平记录列表、结构化 crawl 映射、以及 `{"crawl": {...}}` 包装；旧记录保留为 `legacy`，不会被编造 URL 或准入结论。旧流水线与 `make run` 行为不变。

## 报告内容

`Planner.report()` / CLI `--format summary` 可回答：

- 覆盖范围：`coverage.discovered`、`by_state`、`sitemap_complete`、`truncated`、`scan_failures`、`deletion_inference`；
- 请求节奏：`pace.hosts[host]` 的 `interval` / `next_allowed_at` / `wait_until` / `wait_reason`；
- 额度消耗：`budget.used/spent/reserved/in_flight/released/remaining/next_reset_day`；
- 延期原因：`deferrals[]` 与 `counts` 分桶；
- 版本依据：`policy_versions`、`robots_versions`（每个 `(host, user_agent)` 的版本与到期时间）。

单请求审计用 `Planner.explain(request_id)`：条目状态、账本行、租约历史、事件流。

## 持久化与测试

状态存于项目内 SQLite（`robotscope.db` 默认），测试全部使用内存库或临时目录。测试按模块组织在 `tests/`，`make test` 会自动选择 pytest 或 unittest。
