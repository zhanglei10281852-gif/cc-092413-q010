from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.database import transaction
from app.seismic.service import (
    REHEARSAL_TTL_HOURS,
    GridPoint,
    SeismicService,
    parameter_fingerprint,
)
from app.services.audit import AuditContext, AuditService

PARAM_KEYS = ("model_version", "grid_step_km", "radius_km", "pga_weight", "accepted_score_threshold")
ZONE_LABELS = {
    (1, 1): "东北区",
    (1, -1): "东南区",
    (-1, 1): "西北区",
    (-1, -1): "西南区",
}


class ParameterRehearsalService:
    """烈度计算参数预演、审批与原子发布服务。

    预演阶段只做内存计算，绝不修改生效参数与生产计算结果；发布阶段在单个
    IMMEDIATE 事务内完成旧版本退役与新版本生效。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        if connection is not None:
            self.connection = connection
            self._owns_connection = False
        else:
            from app.database import get_connection

            self.connection = get_connection()
            self._owns_connection = True
        self.audit = AuditService(self.connection, self.clock)

    # ------------------------------------------------------------------ 查询

    def active_version(self) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM seismic_param_versions WHERE status='active' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                "SELECT * FROM seismic_param_versions ORDER BY id LIMIT 1"
            ).fetchone()
        if row is None:
            raise NotFoundError("生效参数版本不存在")
        result = dict(row)
        result["params"] = json.loads(result.pop("params_json"))
        return result

    def list_versions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM seismic_param_versions ORDER BY id DESC"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["params"] = json.loads(item.pop("params_json"))
            result.append(item)
        return result

    def list_rehearsals(self, limit: int = 50) -> list[dict[str, Any]]:
        self._expire_if_due()
        rows = self.connection.execute(
            "SELECT id,status,requested_by,candidate_fingerprint,baseline_version_tag,"
            "expires_at,decided_by,decided_at,published_version_tag,created_at,updated_at "
            "FROM seismic_param_rehearsals ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_rehearsal(self, rehearsal_id: int) -> dict[str, Any]:
        self._expire_if_due()
        row = self.connection.execute(
            "SELECT * FROM seismic_param_rehearsals WHERE id=?", (rehearsal_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("预演不存在")
        return self._hydrate(dict(row))

    def report(self, rehearsal_id: int) -> dict[str, Any]:
        detail = self.get_rehearsal(rehearsal_id)
        return detail["report"]

    # ------------------------------------------------------------------ 预演

    def create_rehearsal(
        self,
        principal: Principal,
        candidate: dict[str, Any],
        event_ids: list[int] | None,
        reason: str,
    ) -> dict[str, Any]:
        self._authorize(principal, "seismic.params.rehearse", None)
        params = self._normalize_candidate(candidate)
        candidate_fp = parameter_fingerprint(params)
        with transaction(immediate=True) as connection:
            self.connection = connection
            self.audit = AuditService(connection, self.clock)
            baseline = self._active_version_row(connection)
            baseline_params = json.loads(baseline["params_json"])
            baseline_fp = baseline["fingerprint"]
            if candidate_fp == baseline_fp:
                raise ValidationError("候选参数与当前生效版本完全一致，无需预演")
            target_events = self._select_events(connection, event_ids)
            if not target_events:
                raise ValidationError("没有可用于预演的代表性事件（事件需存在且含观测数据）")
            per_event, impact = self._evaluate_events(connection, target_events, baseline_params, params)
            report = {
                "input_diff": self._input_diff(baseline, baseline_params, params, candidate_fp),
                "events": per_event,
                "impact": impact,
                "permission_check": self._permission_report(connection, principal),
                "generated_at": to_storage(self.clock.now()),
            }
            now = to_storage(self.clock.now())
            expires_at = to_storage(self.clock.now() + timedelta(hours=REHEARSAL_TTL_HOURS))
            cursor = connection.execute(
                "INSERT INTO seismic_param_rehearsals(status,requested_by,candidate_json,candidate_fingerprint,"
                "baseline_fingerprint,baseline_version_tag,expires_at,event_ids_json,report_json,created_at,updated_at) "
                "VALUES('pending',?,?,?,?,?,?,?,?,?,?)",
                (
                    principal.display_name,
                    json.dumps(params, ensure_ascii=False, sort_keys=True),
                    candidate_fp,
                    baseline_fp,
                    baseline["version_tag"],
                    expires_at,
                    json.dumps([event["id"] for event in target_events]),
                    json.dumps(report, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            rehearsal_id = int(cursor.lastrowid)
            for item in per_event:
                connection.execute(
                    "INSERT INTO seismic_param_rehearsal_events(rehearsal_id,event_id,baseline_digest,"
                    "candidate_digest,points_total,points_changed,max_abs_delta,mean_abs_delta,"
                    "baseline_summary_json,candidate_summary_json,input_diff_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rehearsal_id,
                        item["event_id"],
                        item["baseline_digest"],
                        item["candidate_digest"],
                        item["points_total"],
                        item["points_changed"],
                        item["max_abs_delta"],
                        item["mean_abs_delta"],
                        json.dumps(item["zone_summary"]["baseline"], ensure_ascii=False),
                        json.dumps(item["zone_summary"]["candidate"], ensure_ascii=False),
                        json.dumps(item["input_diff"], ensure_ascii=False),
                    ),
                )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="seismic.rehearsal.create",
                resource_type="seismic_param_rehearsal",
                resource_id=rehearsal_id,
                after={"candidate_fingerprint": candidate_fp, "event_count": len(target_events), "reason": reason},
            )
        return self.get_rehearsal(rehearsal_id)

    # ------------------------------------------------------------------ 审批

    def decide(self, principal: Principal, rehearsal_id: int, decision: str, reason: str) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise ValidationError("decision 只能是 approve 或 reject")
        self._authorize(principal, "seismic.params.approve", rehearsal_id)
        with transaction(immediate=True) as connection:
            self.connection = connection
            self.audit = AuditService(connection, self.clock)
            self._expire_if_due()
            row = connection.execute("SELECT * FROM seismic_param_rehearsals WHERE id=?", (rehearsal_id,)).fetchone()
            if row is None:
                raise NotFoundError("预演不存在")
            if row["status"] != "pending":
                raise ConflictError(f"预演当前状态为 {row['status']}，不能重复审批")
            new_status = "approved" if decision == "approve" else "rejected"
            now = to_storage(self.clock.now())
            connection.execute(
                "UPDATE seismic_param_rehearsals SET status=?,decided_by=?,decided_at=?,decision_reason=?,updated_at=? WHERE id=? AND status='pending'",
                (new_status, principal.display_name, now, reason, now, rehearsal_id),
            )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action=f"seismic.rehearsal.{decision}",
                resource_type="seismic_param_rehearsal",
                resource_id=rehearsal_id,
                after={"decision": decision, "reason": reason},
            )
        return self.get_rehearsal(rehearsal_id)

    # ------------------------------------------------------------------ 发布

    def publish(self, principal: Principal, rehearsal_id: int) -> dict[str, Any]:
        self._authorize(principal, "seismic.params.publish", rehearsal_id)
        with transaction(immediate=True) as connection:
            self.connection = connection
            self.audit = AuditService(connection, self.clock)
            self._expire_if_due()
            rehearsal = connection.execute(
                "SELECT * FROM seismic_param_rehearsals WHERE id=?", (rehearsal_id,)
            ).fetchone()
            if rehearsal is None:
                raise NotFoundError("预演不存在")
            if rehearsal["status"] != "approved":
                raise ConflictError(f"预演状态为 {rehearsal['status']}，只有已批准候选可以发布")
            current = self._active_version_row(connection)
            if current["fingerprint"] != rehearsal["baseline_fingerprint"]:
                raise ConflictError("生效版本在预演之后已变更，候选基线已过期，请重新发起预演")
            candidate = json.loads(rehearsal["candidate_json"])
            candidate_fp = rehearsal["candidate_fingerprint"]
            if parameter_fingerprint(candidate) != candidate_fp:
                raise ConflictError("候选参数指纹校验失败，拒绝发布")
            if connection.execute(
                "SELECT 1 FROM seismic_param_versions WHERE fingerprint=?", (candidate_fp,)
            ).fetchone():
                raise ConflictError("相同参数指纹的版本已存在，不能重复发布")
            now = to_storage(self.clock.now())
            version_tag = f"candidate-{rehearsal_id}-{candidate_fp[:10]}"
            before_active = {
                "version_tag": current["version_tag"],
                "fingerprint": current["fingerprint"],
            }
            connection.execute(
                "UPDATE seismic_param_versions SET status='retired' WHERE id=? AND status='active'",
                (current["id"],),
            )
            cursor = connection.execute(
                "INSERT INTO seismic_param_versions(version_tag,params_json,fingerprint,status,"
                "created_by,published_by,rehearsal_id,created_at,activated_at) VALUES(?,?,?,'active',?,?,?,?,?)",
                (
                    version_tag,
                    json.dumps(candidate, ensure_ascii=False, sort_keys=True),
                    candidate_fp,
                    principal.display_name,
                    principal.display_name,
                    rehearsal_id,
                    now,
                    now,
                ),
            )
            new_version_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE seismic_param_rehearsals SET status='published',published_version_tag=?,updated_at=? WHERE id=? AND status='approved'",
                (version_tag, now, rehearsal_id),
            )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="seismic.rehearsal.publish",
                resource_type="seismic_param_version",
                resource_id=new_version_id,
                before=before_active,
                after={"version_tag": version_tag, "fingerprint": candidate_fp, "rehearsal_id": rehearsal_id},
            )
        return self.get_rehearsal(rehearsal_id)

    # ------------------------------------------------------------------ 内部

    def _authorize(self, principal: Principal, permission: str, rehearsal_id: int | None) -> None:
        if principal.can(permission):
            return
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="seismic.rehearsal.access_denied",
            resource_type="seismic_param_rehearsal",
            resource_id=rehearsal_id,
            outcome="denied",
            metadata={"required_permission": permission},
        )
        raise PermissionDeniedError(f"缺少权限：{permission}")

    def _expire_if_due(self) -> None:
        now = to_storage(self.clock.now())
        rows = self.connection.execute(
            "SELECT id FROM seismic_param_rehearsals WHERE status IN ('pending','approved') AND expires_at < ?",
            (now,),
        ).fetchall()
        for row in rows:
            rehearsal_id = int(row["id"])
            cursor = self.connection.execute(
                "UPDATE seismic_param_rehearsals SET status='expired',updated_at=? WHERE id=? AND status IN ('pending','approved') AND expires_at < ?",
                (now, rehearsal_id, now),
            )
            if cursor.rowcount:
                self.audit.record(
                    AuditContext(None, "system"),
                    action="seismic.rehearsal.expire",
                    resource_type="seismic_param_rehearsal",
                    resource_id=rehearsal_id,
                    metadata={"expires_at_lt": now},
                )

    def _active_version_row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM seismic_param_versions WHERE status='active' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            raise NotFoundError("生效参数版本不存在")
        return row

    def _normalize_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        missing = [key for key in PARAM_KEYS if key not in candidate]
        if missing:
            raise ValidationError(f"候选参数缺少字段：{', '.join(missing)}")
        unknown = [key for key in candidate if key not in PARAM_KEYS]
        if unknown:
            raise ValidationError(f"候选参数包含未知字段：{', '.join(unknown)}")
        params: dict[str, Any] = {}
        for key in PARAM_KEYS:
            value = candidate[key]
            if key == "model_version":
                if not isinstance(value, str) or not 1 <= len(value) <= 40:
                    raise ValidationError("model_version 必须是 1-40 个字符的字符串")
                params[key] = value
            else:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValidationError(f"{key} 必须是数值")
                params[key] = float(value)
        step, radius = params["grid_step_km"], params["radius_km"]
        if not 0 < step <= 100:
            raise ValidationError("grid_step_km 必须在 (0, 100] 范围内")
        if not 0 < radius <= 1000:
            raise ValidationError("radius_km 必须在 (0, 1000] 范围内")
        if step > radius:
            raise ValidationError("grid_step_km 不能大于 radius_km")
        if not 0 <= params["pga_weight"] <= 1:
            raise ValidationError("pga_weight 必须在 [0, 1] 范围内")
        if not 0 <= params["accepted_score_threshold"] <= 1:
            raise ValidationError("accepted_score_threshold 必须在 [0, 1] 范围内")
        return params

    def _select_events(self, connection: sqlite3.Connection, event_ids: list[int] | None) -> list[sqlite3.Row]:
        if event_ids:
            if len(event_ids) > 50:
                raise ValidationError("单次预演最多覆盖 50 个事件")
            placeholders = ",".join("?" for _ in event_ids)
            rows = connection.execute(
                f"SELECT * FROM seismic_events WHERE id IN ({placeholders}) ORDER BY id", event_ids
            ).fetchall()
            if len(rows) != len(set(event_ids)):
                raise NotFoundError("部分代表性事件不存在")
        else:
            rows = connection.execute(
                "SELECT e.* FROM seismic_events e WHERE EXISTS ("
                "SELECT 1 FROM seismic_observations o WHERE o.event_id=e.id) "
                "ORDER BY CASE e.status WHEN 'published' THEN 0 WHEN 'review' THEN 1 ELSE 2 END, e.id DESC LIMIT 20"
            ).fetchall()
        result = []
        for row in rows:
            observations = connection.execute(
                "SELECT * FROM seismic_observations WHERE event_id=? ORDER BY id", (row["id"],)
            ).fetchall()
            if not observations:
                if event_ids:
                    raise ValidationError(f"事件 {row['external_id']} 没有观测数据，不能作为代表性事件")
                continue
            result.append(row)
        return result

    def _evaluate_events(
        self,
        connection: sqlite3.Connection,
        events: list[sqlite3.Row],
        baseline_params: dict[str, Any],
        candidate_params: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        per_event: list[dict[str, Any]] = []
        affected_events = 0
        affected_zones_total = 0
        stale_tasks_total = 0
        published_affected = 0
        for event in events:
            observations = connection.execute(
                "SELECT * FROM seismic_observations WHERE event_id=? ORDER BY id", (event["id"],)
            ).fetchall()
            baseline_points = self._grid(event, observations, baseline_params)
            candidate_points = self._grid(event, observations, candidate_params)
            paired = self._pair_points(baseline_points, candidate_points)
            deltas = [abs(item[2]) for item in paired]
            changed = [item for item in paired if abs(item[2]) > 1e-9]
            baseline_summary = self._zone_summary(baseline_points, event)
            candidate_summary = self._zone_summary(candidate_points, event)
            affected_zones = [
                zone
                for zone in baseline_summary
                if abs(candidate_summary[zone]["mean_intensity"] - baseline_summary[zone]["mean_intensity"]) > 1e-9
                or candidate_summary[zone]["point_count"] != baseline_summary[zone]["point_count"]
            ]
            stale_tasks = connection.execute(
                "SELECT COUNT(*) FROM seismic_computations WHERE event_id=? AND status='done' "
                "AND (model_version<>? OR grid_step_km<>? OR radius_km<>?)",
                (
                    event["id"],
                    candidate_params["model_version"],
                    candidate_params["grid_step_km"],
                    candidate_params["radius_km"],
                ),
            ).fetchone()[0]
            grid_diff = {
                "points_total": len(paired),
                "points_changed": len(changed),
                "max_abs_delta": round(max(deltas), 6) if deltas else 0.0,
                "mean_abs_delta": round(sum(deltas) / len(deltas), 6) if deltas else 0.0,
            }
            is_affected = bool(changed) or bool(affected_zones) or len(baseline_points) != len(candidate_points)
            affected_events += int(is_affected)
            affected_zones_total += len(affected_zones)
            stale_tasks_total += int(stale_tasks)
            published_affected += int(is_affected and event["status"] == "published")
            per_event.append(
                {
                    "event_id": event["id"],
                    "external_id": event["external_id"],
                    "status": event["status"],
                    "magnitude": event["magnitude"],
                    "baseline_digest": self._points_digest(baseline_points),
                    "candidate_digest": self._points_digest(candidate_points),
                    "input_diff": {
                        "observation_count": len(observations),
                        "accepted_baseline": sum(
                            1 for item in observations if float(item["quality_score"]) >= baseline_params["accepted_score_threshold"]
                        ),
                        "accepted_candidate": sum(
                            1 for item in observations if float(item["quality_score"]) >= candidate_params["accepted_score_threshold"]
                        ),
                    },
                    "grid_diff": grid_diff,
                    "zone_summary": {"baseline": baseline_summary, "candidate": candidate_summary},
                    "affected_zones": affected_zones,
                    "stale_done_computations": int(stale_tasks),
                    "affected": is_affected,
                    # 兼容落表列名
                    "points_total": grid_diff["points_total"],
                    "points_changed": grid_diff["points_changed"],
                    "max_abs_delta": grid_diff["max_abs_delta"],
                    "mean_abs_delta": grid_diff["mean_abs_delta"],
                }
            )
        impact = {
            "event_count": len(events),
            "affected_event_count": affected_events,
            "published_event_affected_count": published_affected,
            "affected_zone_count": affected_zones_total,
            "stale_done_computation_count": stale_tasks_total,
            "note": "stale_done_computation_count 为参数切换后需要重算的已完成生产计算任务数量",
        }
        return per_event, impact

    @staticmethod
    def _grid(event: sqlite3.Row, observations: list[sqlite3.Row], params: dict[str, Any]) -> list[GridPoint]:
        return SeismicService._grid_with_params(event, observations, params)

    @staticmethod
    def _pair_points(baseline: list[GridPoint], candidate: list[GridPoint]) -> list[tuple[float, float, float]]:
        remaining = list(candidate)
        paired: list[tuple[float, float, float]] = []
        for point in baseline:
            match = min(remaining, key=lambda other, p=point: (other.latitude - p.latitude) ** 2 + (other.longitude - p.longitude) ** 2, default=None)
            if match is None:
                # 候选网格缩小，该点消失
                paired.append((point.latitude, point.longitude, round(-point.intensity, 6)))
                continue
            remaining.remove(match)
            paired.append((match.latitude, match.longitude, round(match.intensity - point.intensity, 6)))
        for point in remaining:
            # 候选网格扩大，新点出现
            paired.append((point.latitude, point.longitude, point.intensity))
        return paired

    @staticmethod
    def _zone_summary(points: list[GridPoint], event: sqlite3.Row) -> dict[str, dict[str, Any]]:
        center_lat, center_lon = float(event["latitude"]), float(event["longitude"])
        buckets: dict[str, list[float]] = {label: [] for label in ZONE_LABELS.values()}
        for point in points:
            lat_sign = 1 if point.latitude >= center_lat else -1
            lon_sign = 1 if point.longitude >= center_lon else -1
            buckets[ZONE_LABELS[(lat_sign, lon_sign)]].append(point.intensity)
        return {
            zone: {
                "point_count": len(values),
                "mean_intensity": round(sum(values) / len(values), 3) if values else 0.0,
                "max_intensity": round(max(values), 3) if values else 0.0,
            }
            for zone, values in buckets.items()
        }

    @staticmethod
    def _points_digest(points: list[GridPoint]) -> str:
        import hashlib

        payload = json.dumps([point.__dict__ for point in points], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _input_diff(
        self,
        baseline_row: sqlite3.Row,
        baseline_params: dict[str, Any],
        candidate_params: dict[str, Any],
        candidate_fp: str,
    ) -> dict[str, Any]:
        changed_keys = {
            key: {"baseline": baseline_params[key], "candidate": candidate_params[key]}
            for key in PARAM_KEYS
            if baseline_params[key] != candidate_params[key]
        }
        return {
            "baseline_version_tag": baseline_row["version_tag"],
            "baseline_fingerprint": baseline_row["fingerprint"],
            "candidate_fingerprint": candidate_fp,
            "changed_keys": list(changed_keys.keys()),
            "changes": changed_keys,
        }

    def _permission_report(self, connection: sqlite3.Connection, principal: Principal) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT r.code AS role_code, r.name AS role_name, p.code AS permission_code "
            "FROM role_permissions rp JOIN roles r ON r.id=rp.role_id "
            "JOIN permissions p ON p.id=rp.permission_id "
            "WHERE p.code IN ('seismic.params.approve','seismic.params.publish') ORDER BY p.code, r.code"
        ).fetchall()
        approver_roles = sorted({row["role_code"] for row in rows if row["permission_code"] == "seismic.params.approve"})
        publisher_roles = sorted({row["role_code"] for row in rows if row["permission_code"] == "seismic.params.publish"})
        return {
            "requester": principal.display_name,
            "can_approve": principal.can("seismic.params.approve"),
            "can_publish": principal.can("seismic.params.publish"),
            "approver_roles": approver_roles,
            "publisher_roles": publisher_roles,
        }

    def _hydrate(self, row: dict[str, Any]) -> dict[str, Any]:
        row["candidate_params"] = json.loads(row.pop("candidate_json"))
        row["event_ids"] = json.loads(row.pop("event_ids_json"))
        row["report"] = json.loads(row.pop("report_json") or "{}")
        return row
