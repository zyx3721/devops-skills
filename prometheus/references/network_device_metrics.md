# 网络设备 SNMP 指标参考

> 通过 SNMP 采集的网络设备（交换机、路由器、WLAN 控制器）指标。
> 与 node_exporter/categraf 的主机指标完全不同，需单独识别 profile。

## Profile 识别特征

| 信号 | profile |
|------|---------|
| `hwEntity*` + `hwWlan*` + `brand="huawei"` | `huawei_ac` (华为WLAN AC) |
| `hwEntity*` + `brand="huawei"` + 有 `GigabitEthernet` 接口 | `huawei_switch` (华为交换机) |
| `ifHCInOctets` / `ifHCOutOctets` / `ifDescr` | `snmp_standard` (标准SNMP接口) |
| `cisco*` / `cefc*` / `cpm*` | `cisco_device` |
| `h3c*` / `hh3c*` | `h3c_device` |
| 仅有 `up` + `scrape_duration` + 少量指标 | 可能 SNMP 采集不完整 |

## 常见厂商指标族

### 华为 (Huawei) — AC / 交换机

**设备实体指标 (ENTITY-MIB + 华为私有):**
| 指标名 | 含义 | 类型 |
|--------|------|------|
| `hwEntityCpuUsage` | CPU 使用率% | gauge |
| `hwEntityMemUsage` | 内存使用率% | gauge |
| `hwEntityTemperature` | 温度°C | gauge |
| `hwEntityOperStatus` | 运行状态 (1=正常,2=故障,3=正常) | gauge |
| `hwEntityOpticalMode` | 光模块存在/模式 (0=无,非0=有) | gauge |
| `hwEntityUpTime` | 运行时间(秒) | counter |
| `entPhysicalName` | 接口/板卡名(如 GigabitEthernet0/0/1) | label |
| `entPhysicalSoftwareRev` | 固件版本 | label |

**WLAN AC 指标:**
| 指标名 | 含义 | 类型 |
|--------|------|------|
| `hwWlanApCount` | AP 总数 | gauge |
| `hwWlanApNormalRatio` | AP 正常比例% | gauge |
| `hwWlanCurAssocStaNum` | 当前关联终端数 | gauge |
| `hwWlanCurAuthSuccessStaNum` | 认证成功终端数 | gauge |
| `hwWlanCurJointApNum` | 当前上线 AP 数 | gauge |
| `hwWlanIDIndexedApCpuUseRate` | AP CPU 使用率% | gauge |
| `hwWlanIDIndexedApMemoryUseRate` | AP 内存使用率% | gauge |
| `hwWlanIDIndexedApOnlineUserNum` | AP 在线用户数 | gauge |
| `hwWlanIDIndexedApOnlineTime` | AP 在线时长 | gauge |
| `hwWlanIDIndexedApRunState` | AP 运行状态 | gauge |

**存储指标:**
| 指标名 | 含义 |
|--------|------|
| `hwStorageSpace` | 存储总空间(KB) |
| `hwStorageSpaceFree` | 存储可用空间(KB) |
| `hwStorageType` | 存储类型 |

### 标准 SNMP 接口流量 (需额外配置采集)

华为/华三/思科设备如需接口流量，需显式配置采集以下 OID:

| OID | 指标名 | 含义 | 类型 |
|-----|--------|------|------|
| 1.3.6.1.2.1.31.1.1.1.6 | `ifHCInOctets` | 入方向字节数(64-bit) | counter |
| 1.3.6.1.2.1.31.1.1.1.10 | `ifHCOutOctets` | 出方向字节数(64-bit) | counter |
| 1.3.6.1.2.1.2.2.1.11 | `ifInUcastPkts` | 入单播包数 | counter |
| 1.3.6.1.2.1.2.2.1.17 | `ifOutUcastPkts` | 出单播包数 | counter |
| 1.3.6.1.2.1.2.2.1.13 | `ifInDiscards` | 入方向丢包 | counter |
| 1.3.6.1.2.1.2.2.1.19 | `ifOutDiscards` | 出方向丢包 | counter |
| 1.3.6.1.2.1.2.2.1.14 | `ifInErrors` | 入方向错误 | counter |
| 1.3.6.1.2.1.2.2.1.20 | `ifOutErrors` | 出方向错误 | counter |
| 1.3.6.1.2.1.31.1.1.1.15 | `ifHighSpeed` | 接口速率(Mbps) | gauge |
| 1.3.6.1.2.1.2.2.1.2 | `ifDescr` | 接口描述 | label |
| 1.3.6.1.2.1.31.1.1.1.18 | `ifAlias` | 接口别名 | label |

## PromQL 模板 — 网络设备

### 接口流量 (前提: 已采集 ifHCIn/OutOctets)

```
# 入方向速率
rate(ifHCInOctets{instance="X"}[5m])
# 出方向速率
rate(ifHCOutOctets{instance="X"}[5m])
# 带宽利用率 (需 ifHighSpeed)
rate(ifHCInOctets{instance="X"}[5m]) * 8 / (ifHighSpeed{instance="X"} * 1000000) * 100
# Top10 入流量接口
sort_desc(topk(10, rate(ifHCInOctets{instance="X"}[5m])))
```

### 设备实体状态 (华为)

```
# AC CPU
hwEntityCpuUsage{instance="X", entPhysicalName="SRU Board 0"}
# AC 内存
hwEntityMemUsage{instance="X", entPhysicalName="SRU Board 0"}
# 所有板卡温度
hwEntityTemperature{instance="X"}
# 异常状态板卡 (OperStatus != 3)
hwEntityOperStatus{instance="X"} != 3
```

### WLAN 运维 (华为 AC)

```
# 高负载 AP (CPU > 20%)
hwWlanIDIndexedApCpuUseRate{instance="X"} > 20
# Top10 在线用户 AP
sort_desc(topk(10, hwWlanIDIndexedApOnlineUserNum{instance="X"}))
# 用户数趋势
hwWlanCurAssocStaNum{instance="X"}
# AP 正常率
hwWlanApNormalRatio{instance="X"}
```

## 缺失流量指标的排查清单

当用户说"查接口流量"但 Prom 中无相关指标时:

1. 确认 series 中是否有 `ifHC*` / `ifIn*` / `ifOut*` → 没有则说明未采集
2. 确认设备类型: `job` label + `brand` label → 定位厂商
3. 检查 Categraf/SNMP exporter 配置中是否包含 interface OID
4. 华为设备: Entity-MIB 默认常被采集, 但 ifMIB 流量 OID 需显式添加
5. 常见原因: Categraf 的 `snmp.device` 配置只配了 `metrics` 列表但没包含接口流量采集项
