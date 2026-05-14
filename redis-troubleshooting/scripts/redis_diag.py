#!/usr/bin/env python3
"""
Redis 故障一键现场诊断脚本
- 只读操作，不执行任何写命令
- 输出结构化 JSON，便于 AI 解析
- 5 步诊断法：可达性 → CPU/命令热点 → 连接 → 内存/持久化 → 综合判定

用法:
  python3 redis_diag.py --host 10.0.0.1 [--port 6379] [--password XXX]
"""

import argparse
import json
import subprocess
import sys
import re
from typing import Dict, List, Optional, Tuple


def redis_cli(host: str, port: int, password: Optional[str], args: List[str], timeout: int = 10) -> str:
    """执行 redis-cli 命令，返回 stdout"""
    cmd = ["redis-cli", "-h", host, "-p", str(port)]
    if password:
        cmd += ["-a", password, "--no-auth-warning"]
    cmd += args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return f"ERROR: {result.stderr.strip()}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "ERROR: timeout"
    except FileNotFoundError:
        return "ERROR: redis-cli not found"


def parse_info(raw: str) -> Dict[str, str]:
    """解析 Redis INFO 输出为 key-value dict"""
    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            result[k.strip()] = v.strip()
    return result


def parse_commandstats(raw: str) -> List[Dict]:
    """解析 INFO commandstats，按调用次数排序"""
    info = parse_info(raw)
    cmds = []
    for k, v in info.items():
        if k.startswith("cmdstat_"):
            cmd_name = k.replace("cmdstat_", "")
            # 格式: calls=100,usec=500,usec_per_call=5.00
            stats = {}
            for part in v.split(","):
                if "=" in part:
                    pk, pv = part.split("=", 1)
                    try:
                        stats[pk] = float(pv)
                    except ValueError:
                        stats[pk] = pv
            stats["cmd"] = cmd_name
            cmds.append(stats)
    cmds.sort(key=lambda x: x.get("calls", 0), reverse=True)
    return cmds


def parse_slowlog(raw: str) -> List[Dict]:
    """解析 SLOWLOG GET 输出"""
    # SLOWLOG GET 返回格式较复杂，这里做简化解析
    entries = []
    if not raw or raw.startswith("ERROR"):
        return entries
    
    # 尝试以数字开头的条目分割
    current = {}
    for line in raw.splitlines():
        line = line.strip()
        # 每个条目格式: 1) id ... 2) timestamp ... 3) duration ... 4) command ...
        id_match = re.match(r'^\s*\d+\)\s+\(integer\)\s+(\d+)', line)
        if id_match:
            if current:
                entries.append(current)
            current = {"id": int(id_match.group(1))}
        elif "KEYS" in line or "SCAN" in line:
            current["cmd_type"] = "pattern_scan"
            current["raw"] = line
        elif "HGETALL" in line or "LRANGE" in line or "SMEMBERS" in line:
            current["cmd_type"] = "potential_bigkey"
            current["raw"] = line
    
    if current:
        entries.append(current)
    return entries


def parse_client_list(raw: str) -> Dict:
    """解析 CLIENT LIST，统计来源 IP 分布"""
    if not raw or raw.startswith("ERROR"):
        return {"total": 0, "by_ip": []}
    
    lines = raw.strip().splitlines()
    ip_counts = {}
    cmd_counts = {}
    for line in lines:
        fields = {}
        for part in line.split():
            if "=" in part:
                k, v = part.split("=", 1)
                fields[k] = v
        
        addr = fields.get("addr", "unknown")
        ip = addr.rsplit(":", 1)[0] if ":" in addr else addr
        ip_counts[ip] = ip_counts.get(ip, 0) + 1
        
        cmd = fields.get("cmd", "unknown")
        cmd_counts[cmd] = cmd_counts.get(cmd, 0) + 1
    
    by_ip = sorted(ip_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    by_cmd = sorted(cmd_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    
    return {"total": len(lines), "by_ip": by_ip, "by_cmd": by_cmd}


def diagnose(host: str, port: int, password: Optional[str]) -> Dict:
    """执行完整 5 步诊断"""
    result = {
        "target": f"{host}:{port}",
        "steps": {}
    }

    # Step 1: 可达性
    info_server = redis_cli(host, port, password, ["INFO", "server"])
    info_stats = redis_cli(host, port, password, ["INFO", "stats"])
    
    if info_server.startswith("ERROR"):
        result["steps"]["reachability"] = {"status": "unreachable", "error": info_server}
        result["verdict"] = "Redis 不可达，检查网络/进程状态"
        return result
    
    server_info = parse_info(info_server)
    stats_info = parse_info(info_stats)
    
    result["steps"]["reachability"] = {
        "status": "reachable",
        "version": server_info.get("redis_version", "unknown"),
        "uptime_seconds": int(server_info.get("uptime_in_seconds", 0)),
        "ops_per_sec": int(stats_info.get("instantaneous_ops_per_sec", 0)),
        "rejected_connections": int(stats_info.get("rejected_connections", 0)),
    }

    # Step 2: CPU/命令热点
    slowlog = redis_cli(host, port, password, ["SLOWLOG", "GET", "20"])
    cmdstats = redis_cli(host, port, password, ["INFO", "commandstats"])
    
    parsed_slowlog = parse_slowlog(slowlog)
    parsed_cmdstats = parse_commandstats(cmdstats)
    
    # 检查模糊匹配扫描
    pattern_scan_cmds = [c for c in parsed_cmdstats if c["cmd"] in ("keys", "scan")]
    
    result["steps"]["command_hotspot"] = {
        "top_commands": [{"cmd": c["cmd"], "calls": int(c.get("calls", 0)), "usec_per_call": round(c.get("usec_per_call", 0), 2)} for c in parsed_cmdstats[:15]],
        "pattern_scan_detected": len(pattern_scan_cmds) > 0,
        "pattern_scan_details": [{"cmd": c["cmd"], "calls": int(c.get("calls", 0))} for c in pattern_scan_cmds],
        "slowlog_entries": len(parsed_slowlog),
        "slowlog_scan_type": [e for e in parsed_slowlog if e.get("cmd_type") == "pattern_scan"],
    }

    # Step 3: 连接
    info_clients = redis_cli(host, port, password, ["INFO", "clients"])
    client_list = redis_cli(host, port, password, ["CLIENT", "LIST"])
    
    clients_info = parse_info(info_clients)
    parsed_clients = parse_client_list(client_list)
    
    result["steps"]["connections"] = {
        "connected_clients": int(clients_info.get("connected_clients", 0)),
        "client_recent_max_output_buffer": clients_info.get("client_recent_max_output_buffer", "0"),
        "client_distribution": parsed_clients,
    }

    # Step 4: 内存/持久化
    info_memory = redis_cli(host, port, password, ["INFO", "memory"])
    info_persistence = redis_cli(host, port, password, ["INFO", "persistence"])
    
    memory_info = parse_info(info_memory)
    persistence_info = parse_info(info_persistence)
    
    frag_ratio = float(memory_info.get("mem_fragmentation_ratio", 1.0))
    bgsave_time = persistence_info.get("rdb_last_bgsave_time_sec", "0")
    
    result["steps"]["memory_persistence"] = {
        "used_memory_human": memory_info.get("used_memory_human", "unknown"),
        "maxmemory_human": memory_info.get("maxmemory_human", "0"),
        "fragmentation_ratio": frag_ratio,
        "fragmentation_alert": frag_ratio > 1.5,
        "bgsave_status": persistence_info.get("rdb_last_bgsave_status", "unknown"),
        "bgsave_time_sec": bgsave_time,
        "bgsave_slow": int(bgsave_time) > 10 if bgsave_time.isdigit() else False,
    }

    # Step 5: 综合判定
    verdicts = []
    suspected_root = None
    actions = []
    
    # 模糊匹配扫描判定
    if result["steps"]["command_hotspot"]["pattern_scan_detected"]:
        scan_details = result["steps"]["command_hotspot"]["pattern_scan_details"]
        for d in scan_details:
            verdicts.append(f"检测到 {d['cmd']} 命令，调用次数 {d['calls']}，疑似模糊匹配扫描")
        suspected_root = "redis_pattern_scan"
        actions.append("1. redis-cli SLOWLOG GET 20 确认慢命令类型")
        actions.append("2. 与业务方确认是否有模糊匹配接口")
        actions.append("3. 临时禁用/限流该接口")
        actions.append("4. 根治：KEYS→精准查询, SCAN MATCH→无MATCH+客户端过滤")
    
    # Bigkey 判定
    if result["steps"]["command_hotspot"]["slowlog_scan_type"]:
        for e in parsed_slowlog:
            if e.get("cmd_type") == "potential_bigkey":
                verdicts.append(f"慢查询含 bigkey 嫌疑命令: {e.get('raw', 'unknown')}")
                if not suspected_root:
                    suspected_root = "redis_bigkey"
    
    # 连接堆积判定
    connected = result["steps"]["connections"]["connected_clients"]
    if connected > 500:
        verdicts.append(f"连接数 {connected} 较高，可能有连接泄漏")
        actions.append("CLIENT LIST 查看空闲连接来源，评估是否 KILL")
    
    # 持久化阻塞
    if result["steps"]["memory_persistence"]["bgsave_slow"]:
        verdicts.append(f"bgsave 耗时 {bgsave_time}s 超过 10s，可能有 fork 阻塞")
        if not suspected_root:
            suspected_root = "redis_persistence_block"
    
    # 内存碎片
    if result["steps"]["memory_persistence"]["fragmentation_alert"]:
        verdicts.append(f"内存碎片率 {frag_ratio:.2f} > 1.5，建议关注")
    
    result["verdict"] = {
        "suspected_root": suspected_root or "unknown",
        "findings": verdicts if verdicts else ["未发现明显异常"],
        "actions": actions if actions else ["建议持续观察，补充 Prometheus 监控指标"],
    }

    return result


def main():
    parser = argparse.ArgumentParser(description="Redis 故障一键现场诊断")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--password", default=None)
    args = parser.parse_args()

    result = diagnose(args.host, args.port, args.password)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
