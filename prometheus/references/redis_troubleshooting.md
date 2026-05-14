# Redis 模糊匹配扫描故障排障手册

> 源自生产事故复盘 | 2026-05

## 事故画像

| 维度 | 表现 |
|---|---|
| **表象** | 服务侧大量 CLOSE_WAIT → 业务超时异常 |
| **中间层** | Redis CPU 接近 100%，慢查询日志大量 KEYS/SCAN MATCH |
| **根因** | 业务代码高频执行模糊匹配扫描，Redis 单线程被持续占满 |
| **性质** | 非 Redis 自身故障，非持久化/内存淘汰导致 |
| **根治** | KEYS/大量 SCAN → 精准查询后恢复 |

## 异常特征指纹

以下指标组合出现时，高度疑似本类故障：

1. **Redis CPU 逼近 100%**（主机 CPU 可能不高，因为 Redis 只用一个核）
2. **slowlog 持续增长**，且内容为 KEYS/SCAN MATCH
3. **connected_clients 持续攀升**（请求排队，连接无法及时释放）
4. **下游服务 CLOSE_WAIT 堆积**（Redis 处理慢 → 服务端超时 → 连接残留）
5. **业务日志无明显报错**（因为不是 Redis 拒绝连接，而是响应慢）

## 关键认知

### 1. CLOSE_WAIT 是表象，不是根因

CLOSE_WAIT 堆积 = 本端已收到 FIN 但本端应用未关闭连接。常见于：
- 本端线程池满，无法处理关闭逻辑
- 本端阻塞在 I/O 等待（如等待 Redis 响应）

→ 所以排查 CLOSE_WAIT **必须追溯依赖链**，找到上游阻塞点。

### 2. Redis 单线程 ≠ CPU 100% 是资源问题

Redis 单线程模型意味着：
- CPU 100% = 命令处理占满单核
- 不是"CPU 不够"，而是"某些命令太重"
- 堆硬件（升 CPU）无法解决，必须优化命令

### 3. KEYS 和 SCAN 的误区

| 命令 | 复杂度 | 影响 |
|---|---|---|
| KEYS pattern | O(N) 全库扫描 | **生产绝对禁用**，会阻塞整个 Redis |
| SCAN MATCH pattern | O(N) 分步但累计仍全扫 | 单次不阻塞，但高频调用等效于 KEYS |
| GET/HSGET | O(1) | 精准查询，无影响 |

→ SCAN 不是 KEYS 的安全替代品，它只是"不阻塞这一步"，高频调用仍致命。

## Prometheus 指标映射

| Redis 运维维度 | 指标名 | 来源 | 典型异常值 |
|---|---|---|---|
| CPU 使用率 | `process_cpu_seconds_total{process_name="redis-server"}` 或主机的 `100 - cpu_usage_idle` | redis_exporter / categraf | 单核接近 100% |
| 连接数 | `redis_connected_clients` | redis_exporter | 持续增长不回落 |
| 命令速率 | `rate(redis_commands_processed_total[5m])` | redis_exporter | 量级突增 |
| 慢查询 | `increase(redis_slowlog_length[5m])` | redis_exporter | 持续 >0 |
| 内存使用 | `redis_used_memory_bytes` | redis_exporter | 可能正常（非内存问题） |
| CLOSE_WAIT | `netstat_tcp_close_wait`（服务主机） | categraf | 堆积上升 |

### ⚠️ 限制

Prometheus + redis_exporter 的默认配置**不采集按 cmd 分维度的命令统计**（`redis_commands_total{cmd="keys"}`）。需要：
- redis_exporter 启动时加 `--include-latency-metrics` 或配置 `CHECK_KEYS=true`
- 或 Categraf redis 插件开启 command stats 采集
- **若缺此指标，必须降级到 redis-cli 直接查询**（`SLOWLOG GET` / `INFO commandstats`）

## 根因判定流程图

```
服务异常/CLOSE_WAIT 堆积
    │
    ├─→ 服务自身问题？（日志/线程/内存）─→ YES → 服务排障
    │
    └─→ 依赖服务问题？
         │
         ├─→ Redis 可达但响应慢？
         │    │
         │    ├─→ Redis CPU 高？
         │    │    │
         │    │    ├─→ slowlog 含 KEYS/SCAN？
         │    │    │    → ✅ 根因=模糊匹配扫描
         │    │    │    → 止血=限流/禁用
         │    │    │    → 根治=精准查询
         │    │    │
         │    │    ├─→ slowlog 含大 key 操作？
         │    │    │    → 根因=bigkey，拆分处理
         │    │    │
         │    │    └─→ bgsave/fork 导致？
         │    │         → 根因=持久化，优化 save 策略
         │    │
         │    └─→ Redis CPU 不高？
         │         → 网络/内存问题
         │
         └─→ Redis 不可达？
              → Redis 宕机/网络断开
```

## 止血方案（按风险递增）

| # | 操作 | 风险 | 说明 |
|---|---|---|---|
| 1 | 降低慢日志阈值 `CONFIG SET slowlog-log-slower-than 10000` | 无 | 仅增可观测性 |
| 2 | `CLIENT LIST` 找高频客户端 | 无 | 只读 |
| 3 | 与业务方临时禁用模糊匹配接口 | 中 | 需协调 |
| 4 | `CLIENT KILL` 杀特定连接 | 高 | 业务瞬时错误 |
| 5 | `rename-command KEYS ""` 禁用命令 | 很高 | 需重启 + 现有代码报错 |

## 根治清单

- [ ] 代码审查：扫描所有 KEYS/SCAN 调用，确认是否有 pattern 参数
- [ ] KEYS → 改为精准 GET 或维护 key 集合（SET）
- [ ] SCAN + MATCH → 改为 SCAN 无 MATCH 或建立反向索引
- [ ] Redis 配置：`rename-command KEYS ""` 或 `rename-command KEYS __keys_disabled`
- [ ] 告警规则：配置 `redis_slowlog_length` 增长告警
- [ ] 监控补全：开启 redis_exporter 的 cmd 维度指标采集
