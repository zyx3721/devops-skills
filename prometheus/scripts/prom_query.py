#!/usr/bin/env python3
"""
Prometheus 细颗粒度查询与诊断辅助脚本
- Summary First：默认只返回统计摘要
- 基数估算：查询前评估风险
- 画像探测：自动判断 node_exporter / categraf / mixed
- 所有操作 read_only

用法:
  python3 prom_query.py detect_profile [--ident X] [--instance Y]
  python3 prom_query.py estimate --query 'up' [--start ...] [--end ...]
  python3 prom_query.py instant --query 'up' [--result-mode summary]
  python3 prom_query.py range --query 'up' --start ... --end ... [--step ...] [--result-mode summary]
  python3 prom_query.py find_metrics --regex 'node_.+_total'
  python3 prom_query.py label_names
  python3 prom_query.py label_values --label-name instance [--matchers ...]
  python3 prom_query.py series_lookup --matchers 'up{job="node"}'
  python3 prom_query.py targets [--state active] [--only-problematic]
  python3 prom_query.py alerts [--state firing]
  python3 prom_query.py rules [--only-problematic]
  python3 prom_query.py metadata --metric node_cpu_seconds_total
"""

import argparse
import json
import math
import os
import sys
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote
from urllib.error import HTTPError, URLError

# ─── 配置 ───

PROM_URL = os.environ.get("PROMETHEUS_BASE_URL", "http://localhost:9090")
BEARER_TOKEN = os.environ.get("PROMETHEUS_BEARER_TOKEN", "")
BASIC_USER = os.environ.get("PROMETHEUS_BASIC_USER", "")
BASIC_PASS = os.environ.get("PROMETHEUS_BASIC_PASS", "")
TIMEOUT = int(os.environ.get("PROMETHEUS_TIMEOUT_SECONDS", "10"))

# 全局保护参数
MAX_RANGE_SECONDS = 21600     # 6h
MAX_SERIES = 50
MAX_POINTS_PER_SERIES = 600
MAX_RAW_POINTS_RETURNED = 120
DEFAULT_RESULT_MODE = "summary"

# 画像判定指标
CATEGRAF_SIGNALS = ["cpu_usage_idle", "mem_used_percent", "system_load_norm_5",
                    "net_drop_in", "net_drop_out", "disk_inodes_used",
                    "netstat_tcp_time_wait", "system_load_1", "system_load_5", "system_load_15"]
NODE_EXPORTER_SIGNALS = ["node_cpu_seconds_total", "node_memory_MemAvailable_bytes",
                         "node_memory_MemTotal_bytes", "node_filesystem_files_free",
                         "node_network_receive_drop_total", "node_sockstat_TCP_tw"]


# ─── HTTP 客户端 ───

def _make_request(endpoint: str, params: Dict[str, str]) -> Dict:
    """发送只读请求到 Prometheus API"""
    url = f"{PROM_URL}{endpoint}?{urlencode(params)}"
    headers = {}
    if BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {BEARER_TOKEN}"

    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)

    if data.get("status") != "success":
        print(f"Prom error: {data.get('errorType', '?')}: {data.get('error', '?')}", file=sys.stderr)
        sys.exit(1)
    return data.get("data", {})


def _get_label_values(label_name: str, matchers: Optional[List[str]] = None) -> List[str]:
    """获取某 label 的所有值"""
    params = {}
    if matchers:
        params["match[]"] = matchers  # API 支持多个 match[]
    data = _make_request(f"/api/v1/label/{quote(label_name)}/values", params)
    return data if isinstance(data, list) else data.get("data", [])


def _get_series(matchers: List[str], start: Optional[str] = None, end: Optional[str] = None) -> List[Dict]:
    """查询 series labels"""
    params = {"match[]": matchers}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    data = _make_request("/api/v1/series", params)
    return data if isinstance(data, list) else data.get("data", [])


# ─── Summary 计算 ───

def _compute_trend(values: List[float]) -> str:
    """简单趋势判定：up / down / flat"""
    if len(values) < 4:
        return "flat"
    n = len(values)
    first_half = statistics.mean(values[:n//2])
    second_half = statistics.mean(values[n//2:])
    diff_pct = (second_half - first_half) / max(abs(first_half), 1e-9) * 100
    if diff_pct > 10:
        return "up"
    elif diff_pct < -10:
        return "down"
    return "flat"


def _compute_summary(series_data: List[Dict], result_mode: str = "summary",
                     max_series: int = MAX_SERIES,
                     max_raw_points: int = MAX_RAW_POINTS_RETURNED) -> Dict:
    """从 Prometheus range_query 结果计算 summary"""
    total_series = len(series_data)
    truncated = total_series > max_series
    working = series_data[:max_series]

    if result_mode == "raw":
        result_series = []
        for s in working:
            values = [float(v[1]) for v in s.get("values", []) if v[1] != "NaN" and v[1] != "+Inf"]
            # 截断+采样
            if len(values) > max_raw_points:
                head = values[:max_raw_points//4]
                tail = values[-max_raw_points//4:]
                mid_count = max_raw_points - len(head) - len(tail)
                if mid_count > 0 and len(values) > max_raw_points//2:
                    step = max(1, (len(values) - len(head) - len(tail)) // mid_count)
                    mid = values[len(head):-len(tail)][::step][:mid_count]
                else:
                    mid = []
                values = head + mid + tail
            result_series.append({"labels": s.get("metric", {}), "values_raw": values})
        return {"series_count": total_series, "truncated": truncated, "series": result_series}

    # summary 模式
    summaries = []
    anomalies = []
    for s in working:
        values = [float(v[1]) for v in s.get("values", []) if v[1] != "NaN" and v[1] != "+Inf"]
        if not values:
            continue
        trend = _compute_trend(values)
        stats = {
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "avg": round(statistics.mean(values), 2),
            "p50": round(statistics.median(values), 2),
            "p95": round(sorted(values)[int(len(values) * 0.95)] if values else 0, 2),
            "last": round(values[-1], 2),
            "trend": trend,
        }
        # 异常检测：简单 spike 检测
        if len(values) >= 5:
            mean_val = statistics.mean(values)
            std_val = statistics.stdev(values) if len(values) > 1 else 0
            for i, v in enumerate(values):
                if std_val > 0 and abs(v - mean_val) > 3 * std_val:
                    ts = s["values"][i][0] if i < len(s["values"]) else "?"
                    anomalies.append({
                        "labels": s.get("metric", {}),
                        "type": "spike",
                        "at": datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat() if isinstance(ts, (int, float, str)) and str(ts).replace('.','').isdigit() else str(ts),
                        "hint": f"值 {v:.2f} 偏离均值 {mean_val:.2f} 超过 3σ"
                    })
                    break  # 每序列只报一个异常

        summaries.append({"labels": s.get("metric", {}), "stats": stats})

    return {
        "series_count": total_series,
        "top_series": summaries,
        "anomalies": anomalies,
        "truncated": truncated,
        "notes": ["需要更细点位时再改为 result_mode=raw"] if truncated else []
    }


# ─── 具体命令实现 ───

def cmd_detect_profile(args):
    """探测指标画像"""
    now = datetime.now(timezone.utc)
    start = str(int(now.timestamp()) - 3600)
    end = str(int(now.timestamp()))

    # 获取所有指标名
    all_metrics = _get_label_values("__name__")

    found_categraf = [m for m in all_metrics if m in CATEGRAF_SIGNALS]
    found_node = [m for m in all_metrics if m in NODE_EXPORTER_SIGNALS]

    if found_categraf and found_node:
        profile = "mixed"
    elif found_categraf:
        profile = "categraf_system"
    elif found_node:
        profile = "node_exporter"
    else:
        profile = "unknown"

    # 确定 label strategy
    has_ident = False
    has_instance = False
    if found_categraf or found_node:
        sample_matcher = found_categraf[0] if found_categraf else found_node[0]
        series = _get_series([sample_matcher], start, end)
        all_labels = set()
        for s in series[:20]:
            all_labels.update(s.keys())
        has_ident = "ident" in all_labels
        has_instance = "instance" in all_labels

    if has_ident:
        label_strategy = {"primary": "ident", "secondary": "instance" if has_instance else None}
    elif has_instance:
        label_strategy = {"primary": "instance", "secondary": None}
    else:
        label_strategy = {"primary": "instance", "secondary": None}

    result = {
        "profile": profile,
        "label_strategy": label_strategy,
        "signals": {
            "found_metrics": found_categraf + found_node,
            "found_labels": list(filter(None, ["ident" if has_ident else None, "instance" if has_instance else None, "job"]))
        },
        "next_step": "prom_estimate_cardinality"
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_estimate(args):
    """估算查询基数"""
    query = args.query
    start = args.start
    end = args.end
    step = args.step
    max_s = args.max_series or MAX_SERIES
    max_p = args.max_points_per_series or MAX_POINTS_PER_SERIES

    # 用 /api/v1/series 估算序列数
    now = datetime.now(timezone.utc)
    s = start or str(int(now.timestamp()) - 3600)
    e = end or str(int(now.timestamp()))

    try:
        series = _get_series([query], s, e)
    except SystemExit:
        # API 可能不支持所有 PromQL 作为 matcher，回退到 instant query 计数
        data = _make_request("/api/v1/query", {"query": query})
        results = data.get("result", [])
        series = [r.get("metric", {}) for r in results]

    series_count = len(series)

    # 估算点位
    points_per_series = 0
    if start and end:
        range_seconds = int(float(e) - float(s))
        if range_seconds > MAX_RANGE_SECONDS:
            print(json.dumps({
                "risk_level": "high",
                "error": f"range {range_seconds}s exceeds max {MAX_RANGE_SECONDS}s",
                "suggestions": ["缩短时间范围", "增大 step 并重新估算"]
            }, indent=2, ensure_ascii=False))
            return
        step_s = int(step) if step and step != "auto" else max(15, range_seconds // 500)
        points_per_series = range_seconds // step_s
    else:
        points_per_series = 1  # instant

    # 风险判定
    if series_count <= max_s and points_per_series <= max_p:
        risk = "low"
    elif series_count <= max_s * 2 and points_per_series <= max_p * 2:
        risk = "medium"
    else:
        risk = "high"

    suggestions = []
    if series_count > max_s:
        suggestions.append("增加 label 过滤（job/instance/ident/namespace/pod）")
        suggestions.append("用 topk() 或 sum by() 先聚合")
    if points_per_series > max_p:
        suggestions.append("增大 step 或缩短时间范围")

    suggested_step = max(15, int(float(e) - float(s)) // 500) if start and end else 60

    result = {
        "risk_level": risk,
        "estimated_series_upper_bound": series_count,
        "estimated_points_per_series": points_per_series,
        "suggested_step_seconds": suggested_step,
        "suggestions": suggestions
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_instant(args):
    """Instant query"""
    params = {"query": args.query}
    if args.time:
        params["time"] = args.time
    if args.timeout:
        params["timeout"] = f"{args.timeout}s"

    data = _make_request("/api/v1/query", params)
    results = data.get("result", [])

    result_mode = args.result_mode or DEFAULT_RESULT_MODE
    max_s = args.max_series or MAX_SERIES

    if result_mode == "summary":
        total = len(results)
        truncated = total > max_s
        working = results[:max_s]
        top = [{"labels": r.get("metric", {}), "value": float(r.get("value", [0, 0])[1])} for r in working]
        result = {
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "series_count": total,
            "top_series": top,
            "truncated": truncated,
            "notes": ["如 series_count 偏大，建议加 label 过滤或 topk()"] if total > max_s else []
        }
    else:
        # raw — 仍截断
        total = len(results)
        truncated = total > max_s
        working = results[:max_s]
        series_out = [{"labels": r.get("metric", {}), "value": r.get("value", [])} for r in working]
        result = {
            "series_count": total,
            "truncated": truncated,
            "series": series_out
        }

    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_range(args):
    """Range query"""
    if not args.start or not args.end:
        print("range query 需要 --start 和 --end", file=sys.stderr)
        sys.exit(1)

    # 范围检查
    range_seconds = int(float(args.end) - float(args.start))
    if range_seconds > MAX_RANGE_SECONDS:
        print(json.dumps({
            "error": f"range {range_seconds}s 超过上限 {MAX_RANGE_SECONDS}s，请缩短范围或增大 step",
            "max_range_seconds": MAX_RANGE_SECONDS
        }, indent=2, ensure_ascii=False))
        sys.exit(1)

    # step 计算
    if args.step and args.step != "auto":
        step = args.step
    else:
        if range_seconds <= 900:
            step = "15"
        elif range_seconds <= 7200:
            step = "30"
        elif range_seconds <= 21600:
            step = str(max(120, range_seconds // 500))
        else:
            step = str(range_seconds // 500)

    params = {
        "query": args.query,
        "start": args.start,
        "end": args.end,
        "step": f"{step}s" if not step.endswith("s") else step,
    }
    if args.timeout:
        params["timeout"] = f"{args.timeout}s"

    data = _make_request("/api/v1/query_range", params)
    results = data.get("result", [])

    result_mode = args.result_mode or DEFAULT_RESULT_MODE
    max_s = args.max_series or MAX_SERIES
    max_p = args.max_points_per_series or MAX_POINTS_PER_SERIES

    output = _compute_summary(results, result_mode, max_s, MAX_RAW_POINTS_RETURNED if result_mode == "raw" else max_p)
    output["range_seconds"] = range_seconds
    output["step_seconds"] = int(step) if str(step).isdigit() else step

    print(json.dumps(output, indent=2, ensure_ascii=False))


def cmd_find_metrics(args):
    """按正则查找指标名"""
    import re
    all_metrics = _get_label_values("__name__")
    pattern = re.compile(args.regex)
    matched = [m for m in all_metrics if pattern.search(m)]
    limit = args.limit or 200
    truncated = len(matched) > limit
    result = {
        "metric_names": matched[:limit],
        "truncated": truncated
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_label_names(args):
    """列出 label 名"""
    data = _make_request("/api/v1/labels", {})
    labels = data if isinstance(data, list) else data.get("data", [])
    limit = args.limit or 200
    truncated = len(labels) > limit
    print(json.dumps({"label_names": labels[:limit], "truncated": truncated}, indent=2))


def cmd_label_values(args):
    """列出 label 取值"""
    if not args.label_name:
        print("需要 --label-name", file=sys.stderr)
        sys.exit(1)
    params = {}
    if args.matchers:
        params["match[]"] = args.matchers
    data = _make_request(f"/api/v1/label/{quote(args.label_name)}/values", params)
    values = data if isinstance(data, list) else data.get("data", [])
    limit = args.limit or 200
    truncated = len(values) > limit
    print(json.dumps({"values": values[:limit], "truncated": truncated}, indent=2))


def cmd_series_lookup(args):
    """查 series labels"""
    if not args.matchers:
        print("需要 --matchers", file=sys.stderr)
        sys.exit(1)
    now = datetime.now(timezone.utc)
    start = args.start or str(int(now.timestamp()) - 3600)
    end = args.end or str(int(now.timestamp()))
    params = {"match[]": args.matchers, "start": start, "end": end}
    data = _make_request("/api/v1/series", params)
    series = data if isinstance(data, list) else data.get("data", [])
    limit = args.limit or 200
    truncated = len(series) > limit
    print(json.dumps({"series_labels": series[:limit], "truncated": truncated}, indent=2, ensure_ascii=False))


def cmd_targets(args):
    """Targets 摘要"""
    data = _make_request("/api/v1/targets", {})
    active = data.get("activeTargets", [])
    dropped = data.get("droppedTargets", [])

    total = len(active) + len(dropped)
    up_count = sum(1 for t in active if t.get("health") == "up")
    down_list = []

    for t in active:
        if t.get("health") != "up":
            down_list.append({
                "job": t.get("labels", {}).get("job", ""),
                "instance": t.get("labels", {}).get("instance", ""),
                "lastError": t.get("lastError", ""),
                "lastScrape": t.get("lastScrape", "")
            })

    only_problematic = args.only_problematic if hasattr(args, 'only_problematic') else True

    result = {
        "total_targets": total,
        "up_targets": up_count,
        "down_targets": len(down_list),
    }
    if only_problematic:
        result["down_list"] = down_list[:50]
    else:
        result["active_targets"] = active[:50]
        result["down_list"] = down_list

    if args.filter_job:
        result["down_list"] = [d for d in result.get("down_list", []) if d.get("job") == args.filter_job]

    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_alerts(args):
    """Alerts 摘要"""
    data = _make_request("/api/v1/alerts", {})
    alerts = data.get("alerts", [])
    state = args.state or "firing"
    if state != "any":
        alerts = [a for a in alerts if a.get("state") == state]
    limit = args.limit or 50
    truncated = len(alerts) > limit
    output = [{"name": a.get("labels", {}).get("alertname", ""), "labels": a.get("labels", {}), "state": a.get("state", ""), "activeAt": a.get("activeAt", "")} for a in alerts[:limit]]
    print(json.dumps({"alerts": output, "truncated": truncated}, indent=2, ensure_ascii=False))


def cmd_rules(args):
    """Rules 摘要"""
    data = _make_request("/api/v1/rules", {})
    groups = data.get("groups", [])
    rules = []
    for g in groups:
        for r in g.get("rules", []):
            rules.append(r)

    only_problematic = args.only_problematic if hasattr(args, 'only_problematic') else True
    if only_problematic:
        rules = [r for r in rules if r.get("state") in ("firing", "pending") or r.get("type") == "alerting"]

    limit = args.limit or 50
    output = [{"name": r.get("name", ""), "type": r.get("type", ""), "state": r.get("state", "")} for r in rules[:limit]]
    print(json.dumps({"problem_rules": output}, indent=2, ensure_ascii=False))


def cmd_metadata(args):
    """Metric 元数据"""
    if not args.metric:
        print("需要 --metric", file=sys.stderr)
        sys.exit(1)
    data = _make_request("/api/v1/metadata", {"metric": args.metric})
    metadata = data.get(args.metric, [{}])
    result = {"metadata": metadata[0] if metadata else {}}
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ─── 入口 ───

def main():
    parser = argparse.ArgumentParser(description="Prometheus 细颗粒度查询与诊断")
    sub = parser.add_subparsers(dest="command")

    # detect_profile
    p = sub.add_parser("detect_profile")
    p.add_argument("--ident")
    p.add_argument("--instance")

    # estimate
    p = sub.add_parser("estimate")
    p.add_argument("--query", required=True)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--step")
    p.add_argument("--max-series", type=int)
    p.add_argument("--max-points-per-series", type=int)

    # instant
    p = sub.add_parser("instant")
    p.add_argument("--query", required=True)
    p.add_argument("--time")
    p.add_argument("--timeout", type=int)
    p.add_argument("--result-mode", choices=["summary", "raw"])
    p.add_argument("--max-series", type=int)

    # range
    p = sub.add_parser("range")
    p.add_argument("--query", required=True)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--step")
    p.add_argument("--timeout", type=int)
    p.add_argument("--result-mode", choices=["summary", "raw"])
    p.add_argument("--max-series", type=int)
    p.add_argument("--max-points-per-series", type=int)

    # find_metrics
    p = sub.add_parser("find_metrics")
    p.add_argument("--regex", required=True)
    p.add_argument("--limit", type=int)

    # label_names
    p = sub.add_parser("label_names")
    p.add_argument("--limit", type=int)

    # label_values
    p = sub.add_parser("label_values")
    p.add_argument("--label-name", required=True)
    p.add_argument("--matchers", nargs="+")
    p.add_argument("--limit", type=int)

    # series_lookup
    p = sub.add_parser("series_lookup")
    p.add_argument("--matchers", nargs="+", required=True)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--limit", type=int)

    # targets
    p = sub.add_parser("targets")
    p.add_argument("--state", choices=["active", "dropped", "any"], default="active")
    p.add_argument("--only-problematic", action="store_true", default=True)
    p.add_argument("--filter-job")
    p.add_argument("--limit", type=int)

    # alerts
    p = sub.add_parser("alerts")
    p.add_argument("--state", choices=["firing", "pending", "any"], default="firing")
    p.add_argument("--limit", type=int)

    # rules
    p = sub.add_parser("rules")
    p.add_argument("--only-problematic", action="store_true", default=True)
    p.add_argument("--limit", type=int)

    # metadata
    p = sub.add_parser("metadata")
    p.add_argument("--metric", required=True)

    args = parser.parse_args()

    cmds = {
        "detect_profile": cmd_detect_profile,
        "estimate": cmd_estimate,
        "instant": cmd_instant,
        "range": cmd_range,
        "find_metrics": cmd_find_metrics,
        "label_names": cmd_label_names,
        "label_values": cmd_label_values,
        "series_lookup": cmd_series_lookup,
        "targets": cmd_targets,
        "alerts": cmd_alerts,
        "rules": cmd_rules,
        "metadata": cmd_metadata,
    }

    fn = cmds.get(args.command)
    if not fn:
        parser.print_help()
        sys.exit(1)
    fn(args)


if __name__ == "__main__":
    main()
