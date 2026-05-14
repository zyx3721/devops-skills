---
name: redis-troubleshooting
description: "Redis 故障排障技能：覆盖单线程 CPU 飙高、模糊匹配扫描、连接堆积、慢查询、bigkey、持久化阻塞等典型故障模式。与 Prometheus Skill 联动，补充 redis-cli 现场诊断能力。"
metadata:
  audience: 运维工程师
  workflow: 依赖服务排障
  side_effect: read_only（止血方案需人工确认后执行）
  version: 1.0.0
---

# Redis 故障排障技能

## 定位

本 Skill 是 Prometheus Skill 的**下游补充**：
- Prometheus Skill 负责**指标级发现与趋势分析**（远程、非侵入）
- 本 Skill 负责**Redis 现场诊断与根因定位**（需 redis-cli 登录，只读优先）

当 Prometheus 指向 Redis 异常时，加载本 Skill 执行下钻。

---

## 核心认知（生产事故沉淀）

| 认知 | 说明 |
|---|---|
| Redis CPU 100% 几乎总是命令问题 | 单线程模型，CPU 满载 = 命令处理占满，升 CPU 无效 |
| CLOSE_WAIT 是表象 | 堆积必然有上游阻塞，必须追溯依赖链 |
| SCAN≠KEYS 安全替代 | 高频 SCAN MATCH 等效于 KEYS，只是不阻塞单步 |
| Prometheus 可能看不到命令维度 | 默认 redis_exporter 不采集 cmd 级统计，需 redis-cli 补位 |

---

## 故障模式速查

| 模式 | 特征指标 | 根因 | 止血 |
|---|---|---|---|
| **模糊匹配扫描** | CPU 100% + slowlog 含 KEYS/SCAN | 业务代码高频 KEYS/SCAN MATCH | 禁用/限流该接口 |
| **Bigkey 操作** | CPU 突刺 + slowlog 含大 value 操作 | 对大 key 执行复杂操作 | 拆分 bigkey |
| **持久化阻塞** | CPU 突刺 + bgsave 频繁 | RDB/AOF fork 耗时 | 优化 save 策略 |
| **内存淘汰** | used_memory 接近 maxmemory + 键驱逐 | 内存不足 | 扩容或清理 |
| **连接数耗尽** | connected_clients 接近 maxclients | 连接泄漏或突发 | 释放空闲连接 |

---

## 标准排障流程（5 步法）

### Step 1：确认 Redis 是否异常入口

```bash
# 快速检测 Redis 可达性 + 延迟
redis-cli -h <host> -p <port> --latency-history -i 1 | head -5

# 看 INFO 中的关键数字
redis-cli -h <host> -p <port> INFO server | grep -E "uptime_in_seconds|redis_version"
redis-cli -h <host> -p <port> INFO stats | grep -E "total_commands_processed|instantaneous_ops_per_sec|rejected_connections"
```

**判断**：
- `rejected_connections > 0` → maxclients 不够或连接泄漏
- `instantaneous_ops_per_sec` 异常高 → 可能有命令突增

### Step 2：查 CPU 与命令热点

```bash
# 慢查询 TOP 20
redis-cli -h <host> -p <port> SLOWLOG GET 20

# 查看当前正在执行的命令（6.0+）
redis-cli -h <host> -p <port> CLIENT LIST | awk '{print $NF}' | sort | uniq -c | sort -rn | head

# 命令统计（精确定位哪个 cmd 占用最多 CPU）
redis-cli -h <host> -p <port> INFO commandstats
```

**关键判据**：
- `SLOWLOG` 含 `KEYS` / `SCAN` + `MATCH` → **模糊匹配扫描**（本 Skill 重点处理）
- `SLOWLOG` 含 `HGETALL` / `LRANGE` 在大 key 上 → **Bigkey 问题**
- `commandstats` 中 `cmdstat_keys:calls=N` 很高 → 确认高频 KEYS

### Step 3：查连接与 CLOSE_WAIT

```bash
# 连接数与客户端列表
redis-cli -h <host> -p <port> INFO clients | grep connected_clients
redis-cli -h <host> -p <port> CLIENT LIST | wc -l

# 按客户端 IP 分组统计
redis-cli -h <host> -p <port> CLIENT LIST | grep -oP 'addr=\S+' | cut -d: -f1 | sort | uniq -c | sort -rn | head
```

### Step 4：查内存与持久化

```bash
# 内存使用
redis-cli -h <host> -p <port> INFO memory | grep -E "used_memory_human|maxmemory_human|mem_fragmentation_ratio"

# 持久化状态
redis-cli -h <host> -p <port> INFO persistence | grep -E "rdb_last_bgsave_status|aof_last_bgrewrite_status|rdb_last_bgsave_time_sec"
```

**判据**：
- `rdb_last_bgsave_time_sec > 10` → fork 阻塞
- `mem_fragmentation_ratio > 1.5` → 内存碎片

### Step 5：综合判定 + 止血建议

根据 Step 2-4 的证据组合，对照"故障模式速查"表定位根因，按风险递增给止血建议。

---

## 模糊匹配扫描故障：专项处置

### 确认根因

SLOWLOG 或 commandstats 中出现 `KEYS *` / `KEYS prefix:*` / `SCAN 0 MATCH prefix:*` 且调用频率高。

### 止血（read_only → 逐步升级）

```bash
# 1. 无风险：查看谁在调
redis-cli CLIENT LIST | grep -i "cmd=keys\|cmd=scan"

# 2. 低风险：临时降低 slowlog 阈值捕获更多证据
redis-cli CONFIG SET slowlog-log-slower-than 5000

# 3. 中风险：限流（需 Redis 6.2+ 的 acl 或业务侧限流）
# 业务侧：暂时关闭模糊匹配接口或降低调用频率

# 4. 高风险（需确认）：KILL 问题连接
# redis-cli CLIENT KILL addr <ip:port>
```

### 根治

1. **代码改造**：KEYS → 精准 GET；SCAN MATCH → 无 MATCH + 客户端过滤 / 维护 SET 索引
2. **配置加固**：`rename-command KEYS ""` 禁止生产环境使用 KEYS
3. **监控补全**：开启 redis_exporter 的 `--include-latency-metrics` 或 Categraf command stats
4. **告警规则**：`increase(redis_slowlog_length[5m]) > 0` + 通知

---

## 与 Prometheus Skill 联动

| 阶段 | 工具 | 操作 |
|---|---|---|
| 发现 | Prometheus | CPU/连接/慢查询 指标异常 |
| 定位 | redis-cli | SLOWLOG / commandstats 确认命令类型 |
| 止血 | redis-cli | 限流/禁用/KILL |
| 根治 | 代码+配置 | 重构查询逻辑 + rename-command |
| 预防 | Prometheus | 告警规则 + 监控补全 |

---

## 检查清单（排障结尾必过）

- [ ] 根因已定位至具体命令/代码逻辑？
- [ ] 止血操作的风险已评估并获得授权？
- [ ] 根治方案已与业务方确认排期？
- [ ] redis_exporter 是否已补全 cmd 维度指标？
- [ ] 告警规则已配置（slowlog 增长 / CPU / 连接数）？
- [ ] KEYS 命令是否已在生产配置中禁用？
