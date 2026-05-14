---
name: prometheus
description: "Prometheus 细颗粒度查询与诊断技能（原生 Prom + 适配夜莺 Categraf 指标）。流程：先画像、再估算、默认摘要、按需取 raw，避免高基数/大时间范围数据导致 token 爆炸。"
license: MIT
metadata:
  audience: 运维工程师
  workflow: 监控排障
  side_effect: read_only
  token_policy: summary_first
  version: 2.0.0
---

# Prometheus Skill（细颗粒度 + Categraf 适配）

## 核心原则（AI 自律约束）

**这是 read_only 技能。** 只调用 Prometheus HTTP API，不做任何写操作。

**Summary First — 这是铁律，不是建议：**
1. 默认只返回统计摘要（min/max/avg/p95/last/trend），不返回全量 points
2. 任何查询前先评估基数风险，高基数拒绝直查
3. raw 模式永远截断+采样，绝不返回全量矩阵

**推理链路（强制优先级，从上到下）：**
1. `prom_detect_profile` — 探测指标画像（除非用户已明确告知指标来源）
2. `prom_estimate_cardinality` — 估算返回规模（避免高基数爆炸）
3. `prom_range_query(result_mode=summary)` 或 `prom_instant_query` — 执行查询
4. `analyze_trend` / `promql_optimize` / `generate_promql` — 解释/诊断
5. 仅在明确需要时才用 `result_mode=raw`，且 raw 必须截断+采样

> **绝不**直接 query_range 返回全量矩阵。先 profile → estimate → summary。

---

## 适配说明：原生 Prom + 夜莺 Categraf

Prometheus 是原生 Prom，但数据可能同时存在：
- **node_exporter 风格**：`node_cpu_seconds_total`、`node_memory_*`、`node_filesystem_*`、`node_network_*`
- **Categraf/Telegraf 风格**（夜莺生态）：`cpu_usage_idle`、`mem_used_percent`、`system_load_norm_5`、`net_drop_in`

### 指标画像自动判定

| 探测到的指标 | profile |
|---|---|
| `cpu_usage_idle` / `mem_used_percent` / `system_load_norm_5` | `categraf_system` |
| `node_cpu_seconds_total` / `node_memory_*` | `node_exporter` |
| 两者都有 | `mixed`（按对象/label 决策） |
| `hwEntity*` / `hwWlan*` + `brand="huawei"` | `network_device`（华为AC/交换机） |
| `ifHCInOctets` / `ifHCOutOctets` / `ifDescr` | `network_device`（标准SNMP接口） |
| `cisco*` / `cefc*` / `cpm*` | `network_device`（思科） |
| `hh3c*` / `h3c*` | `network_device`（华三） |

> **网络设备画像详情和 PromQL 模板见 `references/network_device_metrics.md`**

### 聚合标签优先级

- series labels 里有 `ident` → 优先用 `ident`（更贴近夜莺对象模型）
- 否则用 `instance`
- 同时存在：默认 `ident`，允许显式选择 `instance`

### 建议（可选但强烈推荐）

在 Categraf 或写入链路增加来源标签（如 `metrics_from="categraf"`），PromQL 更精准、更省 token。

### 网络设备"接口流量"缺失的常见陷阱

用户说"查接口流量"，但 Prom 中没有 `ifHC*`/`node_network_*`/`net_bytes_*` 指标。此时：
1. 先看 series 有没有 `brand`/`job` label → 可能是 SNMP 采集的网络设备（华为/华三/思科），非主机
2. 网络设备默认只采集 Entity-MIB（CPU/内存/温度/光模块），**接口流量 OID（ifMIB）需额外配置**
3. 此时应该: 报告"流量指标缺失"+ 给出设备其他可用指标的综合分析 + 建议补充采集 OID

详见 `references/network_device_metrics.md` 末尾的排查清单。

---

## 全局保护参数（硬限，可覆盖）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `max_range_seconds` | 21600 (6h) | 单次查询最大时间范围 |
| `max_series` | 50 | 单次返回最大序列数 |
| `max_points_per_series` | 600 | 每序列最大点位 |
| `max_raw_points_returned_per_series` | 120 | raw 时每序列最大返回点位 |
| `default_result_mode` | summary | 默认只返回摘要 |
| `truncate_strategy` | head_tail | 截断策略 |
| `auto_step_policy` | 见下表 | 自动 step 计算 |

**auto_step_policy：**
- range ≤ 15m → step=15s
- 15m~2h → step=30~60s
- 2h~6h → step=120~300s
- >6h → 拒绝（必须缩短范围或增大 step 并再次估算）

---

## 必要配置

通过环境变量或 config 提供（不硬编码到 Skill 中）：
- `PROMETHEUS_BASE_URL`：如 `http://localhost:9090`
- `PROMETHEUS_BEARER_TOKEN`（可选）
- `PROMETHEUS_BASIC_USER` / `PROMETHEUS_BASIC_PASS`（可选）
- `PROMETHEUS_TIMEOUT_SECONDS`：默认 10

---

# 能力模块（Actions）

> 所有 Actions 均为 **read_only**。

---

## A. 画像探测

### 1) prom_detect_profile

**用途**：探测当前 Prom 里主机指标来源 + 确定优先 label

**输入**
- `hint_target`（可选）：`{ ident?, instance?, job? }`
- `time_window_seconds`（可选）：默认 3600
- `limit`（可选）：默认 50

**行为**：只做指标名存在性 + 少量 series labels 发现，不拉 points

**输出**
```json
{
  "profile": "node_exporter | categraf_system | mixed | unknown",
  "label_strategy": { "primary": "ident | instance", "secondary": "instance | ident" },
  "signals": {
    "found_metrics": ["cpu_usage_idle", "mem_used_percent", "node_cpu_seconds_total"],
    "found_labels": ["ident", "instance", "job"]
  },
  "next_step": "prom_estimate_cardinality"
}
```

---

## B. 规模估算

### 2) prom_estimate_cardinality

**用途**：查询前估算序列/点位规模，给出风险等级与降基数建议

**输入**
- `query`（必填）：PromQL
- `start`/`end`（可选）：提供则估算点位
- `step_seconds`（可选）：`auto` 或整数
- `max_series`（可选）：默认 50
- `max_points_per_series`（可选）：默认 600

**输出**
```json
{
  "risk_level": "low | medium | high",
  "estimated_series_upper_bound": 120,
  "estimated_points_per_series": 360,
  "suggested_step_seconds": 60,
  "suggestions": [
    "为 query 增加 label 过滤（job/instance/ident/namespace/pod）",
    "用 topk() 或 sum by() 先聚合",
    "缩短时间范围或增大 step"
  ]
}
```

**风险等级判定规则：**
- `low`：series ≤ max_series 且 points ≤ max_points_per_series
- `medium`：series 在 1~2x max_series 或 points 略超
- `high`：series > 2x max_series 或 points > max_points_per_series × 2

---

## C. 取数

### 3) prom_instant_query

**用途**：单点查询当前值/最近值；验证 PromQL 是否可用

**输入**
- `query`（必填）
- `time`（可选，默认 now）
- `timeout_seconds`（可选，默认 10）
- `result_mode`（可选：`summary|raw`，默认 summary）
- `max_series`（可选，默认 50）

**输出（summary）**
```json
{
  "executed_at": "2026-02-26T12:00:00Z",
  "series_count": 18,
  "top_series": [
    { "labels": { "instance": "10.0.0.1:9100" }, "value": 0.92 }
  ],
  "truncated": false,
  "notes": ["如 series_count 偏大，建议加 label 过滤或 topk()"]
}
```

**输出（raw）**：最多返回 `min(max_series, 50)` 条，超出仅 head/tail + 统计

---

### 4) prom_range_query

**用途**：区间趋势查询（排障主力），默认只返回统计摘要

**输入**
- `query`（必填）
- `start`/`end`（必填）
- `step_seconds`（可选：`auto|int`，默认 auto）
- `timeout_seconds`（可选，默认 10）
- `result_mode`（可选：`summary|raw`，默认 summary）
- `max_series`（可选，默认 50）
- `max_points_per_series`（可选，默认 600）

**输出（summary）**
```json
{
  "range_seconds": 3600,
  "step_seconds": 60,
  "points_per_series": 60,
  "series_count": 12,
  "top_series": [
    {
      "labels": { "ident": "host-a" },
      "stats": { "min": 12.1, "max": 88.4, "avg": 43.2, "p50": 41.8, "p95": 81.0, "last": 55.6, "trend": "up|down|flat" }
    }
  ],
  "anomalies": [
    { "labels": { "ident": "host-a" }, "type": "spike", "at": "2026-02-26T11:33:00Z", "hint": "短时尖刺" }
  ],
  "notes": ["需要更细点位时再改为 result_mode=raw（会采样+截断）"]
}
```

**输出（raw）**：每序列最多 `max_raw_points_returned_per_series`（默认120），超出 head/tail + 均匀采样

---

## D. 发现

### 5) prom_find_metrics_by_regex

**用途**：根据正则查指标名（避免全量枚举）

**输入**
- `name_regex`（必填）：如 `^node_.+_total$`
- `limit`（可选，默认 200）

**输出**
```json
{ "metric_names": ["node_cpu_seconds_total", "node_network_receive_bytes_total"], "truncated": false }
```

### 6) prom_list_label_names

**输入**：`limit`（可选，默认 200）

**输出**
```json
{ "label_names": ["job","instance","ident","namespace"], "truncated": false }
```

### 7) prom_list_label_values

**输入**
- `label_name`（必填）
- `matchers`（可选）：如 `["up{job=\"node\"}"]`
- `limit`（可选，默认 200）
- `cursor`（可选：分页游标）

**输出**
```json
{ "values": ["host-a","host-b"], "next_cursor": null, "truncated": false }
```

### 8) prom_series_lookup

**用途**：查 matcher 命中的 series（只返回 labels，不拉 points）

**输入**
- `matchers`（必填）：如 `["cpu_usage_idle{cpu=\"cpu-total\"}"]`
- `start`/`end`（可选，默认最近 1h）
- `limit`（可选，默认 200）

**输出**
```json
{ "series_labels": [ { "ident":"host-a","cpu":"cpu-total" } ], "truncated": false }
```

---

## E. 状态

### 9) prom_targets_summary

**用途**：抓取目标摘要；默认只返回异常 targets

**输入**
- `state`（可选：`active|dropped|any`，默认 active）
- `only_problematic`（可选，默认 true）
- `filter_job`（可选）
- `limit`（可选，默认 50）

**输出**
```json
{
  "total_targets": 120,
  "up_targets": 118,
  "down_targets": 2,
  "down_list": [
    { "job":"node", "instance":"10.0.0.9:9100", "lastError":"context deadline exceeded", "lastScrape":"..." }
  ]
}
```

### 10) prom_alerts_summary

**用途**：当前告警；默认只返回 firing

**输入**
- `state`（可选：`firing|pending|any`，默认 firing）
- `limit`（可选，默认 50）

**输出**
```json
{ "alerts": [ { "name":"HostHighCPU", "labels": { "ident":"host-a" }, "activeAt":"..." } ] }
```

### 11) prom_rules_summary

**用途**：规则摘要；只回问题规则

**输入**
- `rule_type`（可选：`alerting|recording|any`，默认 any）
- `only_problematic`（可选，默认 true）
- `limit`（可选，默认 50）

**输出**
```json
{ "problem_rules": [ { "name":"HostHighCPU", "state":"firing" } ] }
```

### 12) prom_metric_metadata

**用途**：解释指标含义/type/help/unit

**输入**：`metric`（必填）

**输出**
```json
{ "metadata": { "type":"gauge", "help":"...", "unit":"percent" } }
```

---

## F. 轻量分析

### 13) analyze_trend

**用途**：对 range_query summary 做趋势/异常/下一步建议（不接受全量矩阵）

**输入**
- `range_summary`（必填）
- `baseline_summary`（可选）：对比基线

**输出**
```json
{
  "findings": ["host-a CPU p95 在过去1小时上升明显"],
  "suspected_causes": ["突发流量/线程飙升/某进程异常"],
  "next_queries": [
    "topk(5, 100 - cpu_usage_idle{cpu=\"cpu-total\", ident=\"host-a\"})",
    "rate(node_cpu_seconds_total{mode!=\"idle\", instance=\"...\"}[5m])"
  ]
}
```

### 14) generate_promql

**用途**：把自然语言问题描述转成 1~3 条 PromQL

**输入**
- `question`（必填）
- `context`（可选）：`{ job, ident, instance, namespace, pod, service }`
- `profile`（可选）：来自 detect_profile

**输出**
```json
{
  "candidates": [
    { "promql": "...", "purpose": "查看主机CPU使用率", "expected_shape": "vector|matrix" }
  ],
  "guardrails": ["必须限制对象（ident/instance）或先聚合再细化"]
}
```

### 15) promql_optimize

**用途**：优化 PromQL（降基数、降点位、提速）

**输入**
- `promql`（必填）
- `profile`（可选）

**输出**
```json
{
  "optimized_promql": ["...", "..."],
  "why": ["减少 series 数量", "减少昂贵的 label join"],
  "risk": ["聚合后会丢失细分维度"]
}
```

---

## Categraf 适配 PromQL 模板库（常用主机排障）

> 使用前先 `prom_detect_profile`，再按 profile 选模板；补充 `ident/instance/job` 过滤。
> 网络设备模板见 `references/network_device_metrics.md`。

## 1) CPU 使用率
- **categraf_system**：`100 - cpu_usage_idle{cpu="cpu-total"}`
- **node_exporter**：`100 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100`

## 2) Load（归一化 5min）
- **categraf_system**：`system_load_norm_5`
- **node_exporter**：`node_load5 / count by(instance)(node_cpu_seconds_total{mode="idle"})`

## 3) 内存使用率
- **categraf_system**：`mem_used_percent`
- **node_exporter**：`(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100`

## 4) 磁盘 inode 使用率
- **categraf_system**：`disk_inodes_used / disk_inodes_total * 100`
- **node_exporter**：`(1 - node_filesystem_files_free / node_filesystem_files) * 100`

## 5) 网络丢包（1m 增量）
- **categraf_system**：`increase(net_drop_in[1m])` / `increase(net_drop_out[1m])`
- **node_exporter**：`increase(node_network_receive_drop_total[1m])` / `increase(node_network_transmit_drop_total[1m])`

## 6) TCP TIME_WAIT
- **categraf_system**：`netstat_tcp_time_wait`
- **node_exporter**：`node_sockstat_TCP_tw`

## 7) TCP CLOSE_WAIT（连接异常堆积信号）
- **categraf_system**：`netstat_tcp_close_wait`（如有采集）
- **node_exporter**：`node_sockstat_TCP_tw` 不直接等同，需 `node_netstat_Tcp_Closing` 或自定义采集
- **⚠️ 运维经验**：CLOSE_WAIT 堆积通常是**上游问题**的下游表象，而非根因。排查链路应追溯至依赖服务（常见：Redis/DB 慢查询阻塞 → 上游线程池耗尽 → 连接无法正常关闭）。见 `references/redis_troubleshooting.md`。

## 8) Redis 进程级指标（需 redis_exporter 或 Categraf redis 插件）
- **redis_exporter**：`redis_connected_clients`、`redis_used_memory_bytes`、`redis_commands_processed_total`
- **Categraf redis**：`redis_connected_clients`、`redis_used_memory_bytes`、`redis_commands_processed_total`
- **⚠️ 关键**：Redis 单线程模型下，CPU 接近 100% 几乎必然是命令问题而非资源不足。见 `references/redis_troubleshooting.md`。

---

# Redis 模糊匹配扫描故障：PromQL 诊断分支

> 源自生产事故复盘：服务大量 CLOSE_WAIT → 溯源至 Redis CPU 100% → 根因是业务高频执行 KEYS/SCAN MATCH 模糊匹配。

**触发条件**：当出现以下任一组合时进入此分支：
1. 主机 CPU 接近 100% 且进程归属为 redis-server（需 process exporter 或 top 确认）
2. 服务侧大量 CLOSE_WAIT + 依赖链中含 Redis
3. `redis_connected_clients` 持续增长不回落
4. `redis_commands_processed_total` 的 rate 突增 + `redis_slowlog_length` 增长

**诊断 PromQL（优先级从高到低）：**

| 排查步骤 | PromQL | 目的 |
|---|---|---|
| 1. 确认 Redis CPU 是否异常 | `topk(3, 100 - cpu_usage_idle{cpu="cpu-total"})` 或进程级 `process_cpu_seconds_total{process_name="redis-server"}` | 定位问题宿主机 |
| 2. 查 Redis 连接数趋势 | `redis_connected_clients{ident="X"}` | 连接是否堆积 |
| 3. 查命令处理速率 | `rate(redis_commands_processed_total{ident="X"}[5m])` | 是否有突发命令量 |
| 4. 查慢查询 | `increase(redis_slowlog_length{ident="X"}[5m])` | 慢查询是否增长 |
| 5. 查 CLOSE_WAIT | `netstat_tcp_close_wait{ident="服务主机"}` | 确认下游表象 |
| 6. 查 KEYS/SCAN 特征指标 | `rate(redis_commands_total{cmd="keys",ident="X"}[5m])` 或 `rate(redis_commands_total{cmd="scan",ident="X"}[5m])` | **直接定位根因命令** |

**⚠️ 若 Prom 中无 `redis_commands_total` 按 cmd 分维度的指标**（仅靠 redis_exporter 默认配置可能不采集），需：
- 登录 Redis 执行 `SLOWLOG GET 20` 确认慢命令类型
- 或 `INFO commandstats` 查看 cmdstats 找到高频/耗时命令
- 这一步无法纯靠 Prometheus 完成，是关键的**跨工具溯源点**

**根因判定流程：**
```
CLOSE_WAIT 堆积
  → 上游服务超时
    → Redis CPU 100%
      → 慢查询含 KEYS/SCAN MATCH？
        → YES: 根因=业务模糊匹配扫描 → 止血=禁用/限流该命令 → 根治=改为精准查询
        → NO:  继续排查其他 Redis CPU 飙高原因（持久化bgsave/bigkey/内存淘汰）
```

**止血方案（按风险递增）：**
1. `redis-cli CONFIG SET slowlog-log-slower-than 10000` — 降低慢日志阈值以捕获更多证据（无风险）
2. `redis-cli CLIENT LIST` — 找到占连接最多的 client（只读，无风险）
3. 与业务方协商临时禁用模糊匹配接口（需协调，中风险）
4. `redis-cli CLIENT KILL` 杀特定连接（**高风险，可能导致业务瞬时错误**）
5. `redis-cli COMMAND DOCS KEYS` 确认命令存在后考虑 `rename-command` 禁用（**需重启，高风险**）

**根治方向：**
- KEYS → 改用精准 GET/HSGET 或维护 key 集合（SET）
- SCAN MATCH → 改用 SCAN 无 MATCH + 客户端过滤，或建立反向索引
- 必须在代码审查环节禁止 KEYS 命令进入生产

---

# PromQL 降基数守则（必须遵守）

1. **先限制对象**：优先加 `ident="xxx"` 或 `instance="x:port"` 或 `job="xxx"`
2. **先聚合再下钻**：`sum by(ident)` / `avg by(instance)` → 再细分 label
3. **先 topk 再展开**：`topk(10, <expr>)`
4. **对高频 counter 用 rate**：`rate(x_total[5m])` / `increase(x_total[1m])`
5. **区间查询必须控制 step**：step 不要过小；默认 auto

---

# 失败与拒绝策略

| 条件 | 动作 |
|---|---|
| `end-start > max_range_seconds` | 拒绝，要求缩短范围/增大 step，再 estimate |
| `risk_level=high` | 拒绝直查，先返回 suggestions |
| `series_count > max_series` | summary 仅返回 top_series，提示缩小方法 |
| raw 模式 | 永远截断+采样，不返回全量矩阵 |

---

# 实现：API 端点映射

所有 Action 通过 curl 调用 Prometheus HTTP API 实现：

| Action | API 端点 |
|---|---|
| instant_query | `GET /api/v1/query` |
| range_query | `GET /api/v1/query_range` |
| series_lookup | `GET /api/v1/series` |
| list_label_names | `GET /api/v1/labels` |
| list_label_values | `GET /api/v1/label/<name>/values` |
| find_metrics_by_regex | `GET /api/v1/label/__name__/values` + 客户端正则过滤 |
| targets_summary | `GET /api/v1/targets` |
| rules_summary | `GET /api/v1/rules` |
| alerts_summary | `GET /api/v1/alerts` |
| metric_metadata | `GET /api/v1/metadata` |
| detect_profile | 组合调用（label values + series） |
| estimate_cardinality | `GET /api/v1/series` + 客户端计算 |

---

# 示例工作流

## 排查 host-a CPU 异常（混合环境）

1. `prom_detect_profile(hint_target={ident:"host-a"})`
2. `generate_promql(question="host-a CPU 使用率", context={ident:"host-a"}, profile=...)`
3. `prom_estimate_cardinality(query=候选PromQL, start=now-1h, end=now)`
4. `prom_range_query(query=..., start=..., end=..., result_mode="summary")`
5. `analyze_trend(range_summary=...)`

## 告警 firing，先查 Prom targets 健康

1. `prom_alerts_summary(state="firing")`
2. `prom_targets_summary(only_problematic=true, filter_job="node")`
3. 回到 query/analysis 流程定位根因

---

# 实现辅助脚本

当需要调用 Prometheus API 时，参考 `scripts/prom_query.py`（如已创建）或直接用 curl。典型 curl 模式：

```bash
# ⚠️ 注意: curl + match[] 中的花括号/等号/引号需要 URL 编码
# 推荐: 用 -g 禁止 curl glob，并手动编码，或直接用 Python urllib（自动编码）

# 检测配置
PROM_URL="${PROMETHEUS_BASE_URL:-http://localhost:9090}"
AUTH_HEADER=""
[ -n "$PROMETHEUS_BEARER_TOKEN" ] && AUTH_HEADER="Authorization: Bearer $PROMETHEUS_BEARER_TOKEN"

# instant query (简单查询没问题)
curl -s "$PROM_URL/api/v1/query?query=up" ${AUTH_HEADER:+-H "$AUTH_HEADER"} | python3 -m json.tool

# series with match[] — 必须用 -g + URL编码，或用 Python:
# Python: urllib.parse.urlencode({"match[]": '{instance="10.0.0.1"}'}) → 自动编码
# curl:   curl -g -s 'http://..../series?match%5B%5D=%7Binstance%3D%2210.0.0.1%22%7D'
# 经验: 涉及 match[] 时优先用 Python urllib，避免 curl 花括号/引号嵌套地狱

# range query (summary computation in python)
curl -s "$PROM_URL/api/v1/query_range?query=up&start=...&end=...&step=60s" ${AUTH_HEADER:+-H "$AUTH_HEADER"}
```

summary 的统计计算（min/max/avg/p95/trend）在客户端 Python 中完成，不依赖 Prom 端。
