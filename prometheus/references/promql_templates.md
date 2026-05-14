# Categraf + node_exporter PromQL 模板速查

> 使用前先 `prom_detect_profile` 确定 profile，再按表选模板；补充 `ident/instance/job` 过滤。

## CPU

| 场景 | categraf_system | node_exporter |
|---|---|---|
| CPU 使用率 | `100 - cpu_usage_idle{cpu="cpu-total"}` | `100 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100` |
| CPU 各模式分布 | `cpu_usage{cpu="cpu-total"}` 按 mode 分 | `rate(node_cpu_seconds_total{cpu="cpu-total"}[5m]) * 100` |
| Top5 高 CPU | `topk(5, 100 - cpu_usage_idle{cpu="cpu-total"})` | `topk(5, 100 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)` |

## Load

| 场景 | categraf_system | node_exporter |
|---|---|---|
| 归一化 5min load | `system_load_norm_5` | `node_load5 / count by(instance)(node_cpu_seconds_total{mode="idle"})` |
| 1min/5min/15min | `system_load_1` / `system_load_5` / `system_load_15` | `node_load1` / `node_load5` / `node_load15` |

## 内存

| 场景 | categraf_system | node_exporter |
|---|---|---|
| 内存使用率 | `mem_used_percent` | `(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100` |
| Swap 使用率 | `swap_used_percent` | `(1 - node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes) * 100` |
| 可用内存 | `mem_available` | `node_memory_MemAvailable_bytes` |

## 磁盘

| 场景 | categraf_system | node_exporter |
|---|---|---|
| 磁盘使用率 | `disk_used_percent` | `(1 - node_filesystem_avail_bytes / node_filesystem_size_bytes) * 100` |
| inode 使用率 | `disk_inodes_used / disk_inodes_total * 100` | `(1 - node_filesystem_files_free / node_filesystem_files) * 100` |
| 读延迟 | `disk_read_time_ms` | `rate(node_disk_read_time_seconds_total[5m])` |

## 网络

| 场景 | categraf_system | node_exporter |
|---|---|---|
| 入方向丢包 | `increase(net_drop_in[1m])` | `increase(node_network_receive_drop_total[1m])` |
| 出方向丢包 | `increase(net_drop_out[1m])` | `increase(node_network_transmit_drop_total[1m])` |
| 带宽使用 | `rate(net_bytes_recv[5m])` / `rate(net_bytes_sent[5m])` | `rate(node_network_receive_bytes_total[5m])` / `rate(node_network_transmit_bytes_total[5m])` |

## TCP

| 场景 | categraf_system | node_exporter |
|---|---|---|
| TIME_WAIT | `netstat_tcp_time_wait` | `node_sockstat_TCP_tw` |
| ESTABLISHED | `netstat_tcp_established` | `node_sockstat_TCP_established` |
| 连接数趋势 | `netstat_tcp_time_wait` (range) | `node_sockstat_TCP_tw` (range) |

## 降基数速查

| 技巧 | 示例 |
|---|---|
| 限制对象 | `{ident="host-a"}` / `{instance="10.0.0.1:9100"}` |
| 先聚合 | `sum by(ident)(...)` / `avg by(instance)(...)` |
| TopK | `topk(5, ...)` / `bottomk(5, ...)` |
| Rate | `rate(counter[5m])` / `increase(counter[1h])` |
| 控制步长 | step=60s（1h 内）/ step=300s（6h 内） |
