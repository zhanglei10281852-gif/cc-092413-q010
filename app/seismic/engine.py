"""烈度计算内核。

把原来写死在 SeismicService 里的质量判定与网格烈度公式抽成参数驱动的纯函数，
便于在不触碰生产数据的前提下对候选参数集进行预演对比。
BASELINE_PARAMS 与历史硬编码常量严格一致，现有计算路径行为不变。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# 生效参数的初始基线，取值与基线版本代码中的常量完全相同。
BASELINE_PARAMS: dict[str, Any] = {
    "model_version": "gmpe-2026.1",
    "grid_step_km": 10.0,
    "radius_km": 100.0,
    "pga_limit": 20.0,
    "pgv_limit": 300.0,
    "quality_accept_score": 0.6,
    "pga_weight": 0.01,
}

PARAMETER_FIELDS: tuple[str, ...] = tuple(BASELINE_PARAMS.keys())


@dataclass(frozen=True)
class GridPoint:
    latitude: float
    longitude: float
    intensity: float


def canonical_json(params: Mapping[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parameters_digest(params: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(params).encode()).hexdigest()


def classify(observation: Mapping[str, Any], params: Mapping[str, Any]) -> tuple[float, str, str]:
    """按候选参数评估单条观测质量，返回 (分数, 状态, 原因)。"""
    reasons: list[str] = []
    score = 1.0
    if observation.get("pga") is None and observation.get("pgv") is None:
        score = 0.0
        reasons.append("缺少峰值指标")
    if observation.get("pga") is not None and float(observation["pga"]) > float(params["pga_limit"]):
        score -= 0.6
        reasons.append("PGA 超出量程")
    if observation.get("pgv") is not None and float(observation["pgv"]) > float(params["pgv_limit"]):
        score -= 0.4
        reasons.append("PGV 超出量程")
    if float(observation.get("distance_km", 0)) == 0:
        score -= 0.2
        reasons.append("距离为零")
    score = max(0.0, min(1.0, round(score, 3)))
    status = "accepted" if score >= float(params["quality_accept_score"]) else "rejected"
    return score, status, "、".join(reasons) if reasons else "通过基础质量检查"


def grid_points(
    event: Mapping[str, Any],
    accepted_observations: Sequence[Mapping[str, Any]],
    params: Mapping[str, Any],
) -> list[GridPoint]:
    """用指定参数计算烈度网格；调用方负责传入已被该参数判定为 accepted 的观测。"""
    step = float(params["grid_step_km"])
    radius = float(params["radius_km"])
    pga_weight = float(params["pga_weight"])
    center_lat, center_lon = float(event["latitude"]), float(event["longitude"])
    radius_deg = radius / 111.0
    count = max(1, int(math.floor((radius * 2) / step)))
    result: list[GridPoint] = []
    for lat_index in range(count + 1):
        lat = center_lat - radius_deg + lat_index * (step / 111.0)
        for lon_index in range(count + 1):
            lon = center_lon - radius_deg + lon_index * (step / 111.0) / max(0.2, math.cos(math.radians(lat)))
            values = []
            for item in accepted_observations:
                distance = math.hypot(
                    (lat - center_lat) * 111,
                    (lon - center_lon) * 111 * max(0.2, math.cos(math.radians(lat))),
                )
                weight = 1 / max(1, abs(distance - float(item["distance_km"])))
                estimate = (
                    float(event["magnitude"])
                    - math.log10(max(1, float(item["distance_km"])))
                    + (float(item["pga"] or 0) * pga_weight)
                )
                values.append((estimate * weight, weight))
            intensity = (
                round(sum(value for value, _ in values) / sum(weight for _, weight in values), 3)
                if values
                else round(float(event["magnitude"]) - 1, 3)
            )
            result.append(GridPoint(round(lat, 6), round(lon, 6), intensity))
    return result


def zone_summary(points: Sequence[GridPoint]) -> dict[str, dict[str, Any]]:
    """把网格点按整数烈度带汇总为分区摘要。"""
    zones: dict[int, list[GridPoint]] = {}
    for point in points:
        zones.setdefault(int(math.floor(point.intensity)), []).append(point)
    summary: dict[str, dict[str, Any]] = {}
    for band, members in sorted(zones.items()):
        summary[str(band)] = {
            "point_count": len(members),
            "max_intensity": round(max(item.intensity for item in members), 3),
            "min_intensity": round(min(item.intensity for item in members), 3),
            "centroid_lat": round(sum(item.latitude for item in members) / len(members), 4),
            "centroid_lon": round(sum(item.longitude for item in members) / len(members), 4),
        }
    return summary


def run_event(event: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], params: Mapping[str, Any]) -> dict[str, Any]:
    """对单个事件做一次完整的内存计算，不写入任何生产表。"""
    classified: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    for item in observations:
        score, status, reason = classify(item, params)
        projected = dict(item)
        projected["quality_score"] = score
        projected["quality_status"] = status
        projected["quality_reason"] = reason
        classified.append(projected)
        if status == "accepted":
            accepted.append(projected)
    points = grid_points(event, accepted, params)
    return {
        "observations": [
            {
                "station_code": item["station_code"],
                "channel": item["channel"],
                "observed_at": item["observed_at"],
                "quality_score": item["quality_score"],
                "quality_status": item["quality_status"],
                "quality_reason": item["quality_reason"],
            }
            for item in classified
        ],
        "points": [point.__dict__ for point in points],
        "zones": zone_summary(points),
    }


def _observation_key(item: Mapping[str, Any]) -> str:
    return f"{item['station_code']}|{item['channel']}|{item['observed_at']}"


def compare_event(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """对比同一事件在基线与候选参数下的输入差异、结果差异与分区差异。"""
    before_obs = {_observation_key(item): item for item in baseline["observations"]}
    after_obs = {_observation_key(item): item for item in candidate["observations"]}
    flipped: list[dict[str, Any]] = []
    score_changed = 0
    for key in sorted(before_obs.keys() | after_obs.keys()):
        old = before_obs.get(key)
        new = after_obs.get(key)
        if old is None or new is None:
            continue
        if old["quality_status"] != new["quality_status"]:
            flipped.append(
                {
                    "station_code": new["station_code"],
                    "channel": new["channel"],
                    "observed_at": new["observed_at"],
                    "before_status": old["quality_status"],
                    "after_status": new["quality_status"],
                    "before_score": old["quality_score"],
                    "after_score": new["quality_score"],
                }
            )
        elif old["quality_score"] != new["quality_score"]:
            score_changed += 1

    before_points = {(p["latitude"], p["longitude"]): p["intensity"] for p in baseline["points"]}
    after_points = {(p["latitude"], p["longitude"]): p["intensity"] for p in candidate["points"]}
    shared = before_points.keys() & after_points.keys()
    deltas = [
        abs(after_points[key] - before_points[key])
        for key in shared
        if abs(after_points[key] - before_points[key]) > 1e-9
    ]
    added_point_count = len(after_points.keys() - before_points.keys())
    removed_point_count = len(before_points.keys() - after_points.keys())
    added_zones = sorted(set(candidate["zones"]) - set(baseline["zones"]))
    removed_zones = sorted(set(baseline["zones"]) - set(candidate["zones"]))
    changed_zones = []
    for band in sorted(set(baseline["zones"]) & set(candidate["zones"])):
        if baseline["zones"][band] != candidate["zones"][band]:
            changed_zones.append(
                {"zone": band, "baseline": baseline["zones"][band], "candidate": candidate["zones"][band]}
            )
    return {
        "input_diff": {
            "observations_total": len(baseline["observations"]),
            "baseline_accepted": sum(1 for item in baseline["observations"] if item["quality_status"] == "accepted"),
            "candidate_accepted": sum(1 for item in candidate["observations"] if item["quality_status"] == "accepted"),
            "flipped": flipped,
            "score_changed": score_changed,
        },
        "result_diff": {
            "point_count": len(after_points),
            "changed_points": len(deltas),
            "added_points": added_point_count,
            "removed_points": removed_point_count,
            "max_abs_delta": round(max(deltas), 4) if deltas else 0.0,
            "mean_abs_delta": round(sum(deltas) / len(deltas), 4) if deltas else 0.0,
            "baseline_zones": baseline["zones"],
            "candidate_zones": candidate["zones"],
            "zone_diff": {"added": added_zones, "removed": removed_zones, "changed": changed_zones},
        },
    }
