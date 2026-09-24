"""烈度参数预演（候选参数集）服务。

设计要点：
- 预演只读取生产事件/观测并写入独立的预演表，当前生效参数（seismic_active_params）
  在显式发布前绝不改变；
- 报告在创建预演时一次性生成并落库，包含输入差异、结果差异、预计影响范围与权限检查；
- 审批（approve/reject）与发布（publish）分离，发布在单一一笔即时事务内原子切换生效版本；
- 重复确认、重复发布、过期候选一律安全失败（409），失败尝试的审计与阶段日志会先落库再返回；
- 所有状态都持久化在 SQLite，服务重启不会让 staged/approved 候选变成已发布。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import timedelta
from typing import Any, Callable

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.seismic.engine import (
    BASELINE_PARAMS,
    PARAMETER_FIELDS,
    canonical_json,
    compare_event,
    parameters_digest,
    run_event,
)
from app.services.audit import AuditContext, AuditService

REHEARSE_PERMISSION = "seismic.params.rehearse"
APPROVE_PERMISSION = "seismic.params.approve"
PUBLISH_PERMISSION = "seismic.params.publish"
READ_PERMISSION = "seismic.params.read"

DEFAULT_SAMPLE_LIMIT = 5
MAX_SAMPLE_LIMIT = 20
MAX_TTL_MINUTES = 7 * 24 * 60

REHEARSAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS seismic_param_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_tag TEXT NOT NULL UNIQUE,
    params_json TEXT NOT NULL,
    params_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('staged','approved','rejected','active','superseded','expired')),
    sample_event_ids TEXT NOT NULL DEFAULT '[]',
    report_json TEXT NOT NULL DEFAULT '{}',
    created_by_user_id INTEGER,
    created_by_name TEXT NOT NULL,
    approved_by_user_id INTEGER,
    approved_by_name TEXT,
    approved_at TEXT,
    rejected_by_user_id INTEGER,
    rejected_by_name TEXT,
    rejected_at TEXT,
    reject_reason TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    published_at TEXT,
    published_by_user_id INTEGER,
    published_by_name TEXT,
    supersedes_version_tag TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_active_params (
    id INTEGER PRIMARY KEY CHECK(id=1),
    version_tag TEXT NOT NULL,
    params_json TEXT NOT NULL,
    params_digest TEXT NOT NULL,
    activated_version_id INTEGER REFERENCES seismic_param_versions(id),
    activated_by_user_id INTEGER,
    activated_by_name TEXT NOT NULL DEFAULT 'system',
    activated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_param_stage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES seismic_param_versions(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    actor_user_id INTEGER,
    actor_name TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','denied','failure')),
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_param_versions_status ON seismic_param_versions(status, created_at);
"""


def default_ttl_minutes() -> int:
    return int(os.getenv("TOWNSHIP_SEISMIC_CANDIDATE_TTL_MINUTES", "1440"))


class ParameterRehearsalService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ schema
    def ensure_schema(self) -> None:
        self.connection.executescript(REHEARSAL_SCHEMA)
        # 首次启用时，把历史硬编码常量登记为初始生效版本（基线）。
        if self.connection.execute("SELECT 1 FROM seismic_active_params WHERE id=1").fetchone():
            return
        now = to_storage(self.clock.now())
        baseline_digest = parameters_digest(BASELINE_PARAMS)
        cursor = self.connection.execute(
            "INSERT INTO seismic_param_versions(version_tag,params_json,params_digest,status,report_json,"
            "created_by_name,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "baseline-gmpe-2026.1",
                canonical_json(BASELINE_PARAMS),
                baseline_digest,
                "active",
                json.dumps({"baseline": True}, ensure_ascii=False),
                "system",
                "9999-12-31T23:59:59+00:00",
                now,
                now,
            ),
        )
        self.connection.execute(
            "INSERT INTO seismic_active_params(id,version_tag,params_json,params_digest,activated_version_id,"
            "activated_by_user_id,activated_by_name,activated_at) VALUES(1,?,?,?,?,?,?,?)",
            (
                "baseline-gmpe-2026.1",
                canonical_json(BASELINE_PARAMS),
                baseline_digest,
                cursor.lastrowid,
                None,
                "system",
                now,
            ),
        )

    # ------------------------------------------------------------- active read
    def active_version(self) -> dict[str, Any]:
        self.ensure_schema()
        return self._read_active()

    def _read_active(self) -> dict[str, Any]:
        """读取生效版本；调用方需已执行 ensure_schema（避免在事务内触发 executescript）。"""
        row = self.connection.execute("SELECT * FROM seismic_active_params WHERE id=1").fetchone()
        if row is None:
            return {
                "version_tag": "baseline-gmpe-2026.1",
                "params": dict(BASELINE_PARAMS),
                "params_digest": parameters_digest(BASELINE_PARAMS),
                "activated_version_id": None,
                "activated_at": None,
                "activated_by_name": "system",
            }
        return {
            "version_tag": row["version_tag"],
            "params": json.loads(row["params_json"]),
            "params_digest": row["params_digest"],
            "activated_version_id": row["activated_version_id"],
            "activated_at": row["activated_at"],
            "activated_by_name": row["activated_by_name"],
        }

    # ------------------------------------------------------------ rehearsal run
    def create_rehearsal(
        self,
        principal: Principal,
        candidate: dict[str, Any],
        *,
        event_ids: list[int] | None = None,
        sample_limit: int = DEFAULT_SAMPLE_LIMIT,
        ttl_minutes: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        self._require(principal, REHEARSE_PERMISSION, "seismic.param_rehearsal", None)
        self.ensure_schema()
        sample_limit = max(1, min(int(sample_limit), MAX_SAMPLE_LIMIT))
        minutes = ttl_minutes if ttl_minutes is not None else default_ttl_minutes()
        if not 1 <= minutes <= MAX_TTL_MINUTES:
            raise ValidationError("候选有效期必须在 1 分钟到 7 天之间")

        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            active = self._read_active()
            params = self._merge_and_validate(candidate, active["params"])
            if parameters_digest(params) == active["params_digest"]:
                raise ValidationError("候选参数集与当前生效参数完全一致，无需预演")
            sample_ids = self._resolve_sample_events(event_ids, sample_limit)
            if not sample_ids:
                raise ValidationError("没有可用于预演的代表性事件")

            now = self.clock.now()
            now_text = to_storage(now)
            expires_at = to_storage(now + timedelta(minutes=minutes))
            report = self._build_report(active["params"], params, sample_ids, principal)
            digest = parameters_digest(params)
            version_tag = f"cand-{digest[:12]}-{secrets.token_hex(3)}"

            cursor = connection.execute(
                "INSERT INTO seismic_param_versions(version_tag,params_json,params_digest,status,"
                "sample_event_ids,report_json,created_by_user_id,created_by_name,expires_at,"
                "supersedes_version_tag,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_tag,
                    canonical_json(params),
                    digest,
                    "staged",
                    json.dumps(sample_ids),
                    json.dumps(report, ensure_ascii=False, sort_keys=True),
                    principal.user_id,
                    principal.display_name,
                    expires_at,
                    active["version_tag"],
                    now_text,
                    now_text,
                ),
            )
            version_id = int(cursor.lastrowid)
            self._stage_log(connection, version_id, "create", principal, "success", {"reason": reason, "ttl_minutes": minutes})
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="seismic.rehearsal.create",
                resource_type="seismic_param_version",
                resource_id=version_id,
                after={"version_tag": version_tag, "params": params, "sample_event_ids": sample_ids, "expires_at": expires_at},
                metadata={"reason": reason, "baseline_version": active["version_tag"]},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return self.detail(version_id)

    def _build_report(
        self,
        baseline_params: dict[str, Any],
        candidate_params: dict[str, Any],
        event_ids: list[int],
        principal: Principal,
    ) -> dict[str, Any]:
        parameter_diff = {
            field: {"baseline": baseline_params[field], "candidate": candidate_params[field]}
            for field in PARAMETER_FIELDS
            if baseline_params[field] != candidate_params[field]
        }
        events_report: list[dict[str, Any]] = []
        affected_event_ids: list[int] = []
        affected_published: list[str] = []
        all_changed_points = 0
        all_flipped = 0
        max_delta = 0.0
        affected_zones: set[str] = set()

        for event_id in event_ids:
            event = self.connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone()
            if event is None:
                continue
            observations = [
                dict(item)
                for item in self.connection.execute(
                    "SELECT * FROM seismic_observations WHERE event_id=? ORDER BY id", (event_id,)
                ).fetchall()
            ]
            baseline_run = run_event(dict(event), observations, baseline_params)
            candidate_run = run_event(dict(event), observations, candidate_params)
            diff = compare_event(baseline_run, candidate_run)
            changed_points = diff["result_diff"]["changed_points"]
            added_points = diff["result_diff"]["added_points"]
            removed_points = diff["result_diff"]["removed_points"]
            flipped = len(diff["input_diff"]["flipped"])
            zone_diff = diff["result_diff"]["zone_diff"]
            touched_zones = set(zone_diff["added"]) | set(zone_diff["removed"]) | {
                item["zone"] for item in zone_diff["changed"]
            }
            is_affected = (
                changed_points > 0 or added_points > 0 or removed_points > 0 or flipped > 0 or bool(touched_zones)
            )
            if is_affected:
                affected_event_ids.append(event_id)
                if event["status"] == "published":
                    affected_published.append(event["external_id"])
                all_changed_points += changed_points + added_points + removed_points
                all_flipped += flipped
                max_delta = max(max_delta, diff["result_diff"]["max_abs_delta"])
                affected_zones.update(touched_zones)
            events_report.append(
                {
                    "event_id": event_id,
                    "external_id": event["external_id"],
                    "status": event["status"],
                    "magnitude": event["magnitude"],
                    "affected": is_affected,
                    "input_diff": diff["input_diff"],
                    "result_diff": {
                        key: value
                        for key, value in diff["result_diff"].items()
                        if key not in {"baseline_zones", "candidate_zones"}
                    },
                    "zone_summaries": {
                        "baseline": baseline_run["zones"],
                        "candidate": candidate_run["zones"],
                    },
                }
            )

        impact = {
            "events_evaluated": len(events_report),
            "events_affected": len(affected_event_ids),
            "affected_event_ids": affected_event_ids,
            "published_events_affected": affected_published,
            "observations_reclassified": all_flipped,
            "grid_points_changed": all_changed_points,
            "max_abs_intensity_delta": round(max_delta, 4),
            "affected_zone_bands": sorted(affected_zones),
        }
        return {
            "baseline_version": self._read_active()["version_tag"],
            "baseline_params": baseline_params,
            "candidate_params": candidate_params,
            "candidate_digest": parameters_digest(candidate_params),
            "input_diff": {"parameters": parameter_diff, "changed_parameter_count": len(parameter_diff)},
            "events": events_report,
            "impact_scope": impact,
            "permission_check": self._permission_report(principal),
            "generated_at": to_storage(self.clock.now()),
        }

    def _permission_report(self, principal: Principal) -> dict[str, Any]:
        roles = [
            dict(item)
            for item in self.connection.execute(
                "SELECT r.id,r.code,r.name FROM roles r "
                "JOIN role_permissions rp ON rp.role_id=r.id "
                "JOIN permissions p ON p.id=rp.permission_id "
                "WHERE p.code=? ORDER BY r.code",
                (PUBLISH_PERMISSION,),
            ).fetchall()
        ]
        publishers = [
            {"user_id": item["id"], "username": item["username"], "display_name": item["display_name"]}
            for item in self.connection.execute(
                "SELECT DISTINCT u.id,u.username,u.display_name FROM users u "
                "JOIN user_roles ur ON ur.user_id=u.id "
                "JOIN role_permissions rp ON rp.role_id=ur.role_id "
                "JOIN permissions p ON p.id=rp.permission_id "
                "WHERE p.code=? AND u.status='active' ORDER BY u.id",
                (PUBLISH_PERMISSION,),
            ).fetchall()
        ]
        return {
            "required_permission": PUBLISH_PERMISSION,
            "approve_permission": APPROVE_PERMISSION,
            "roles_that_may_publish": roles,
            "active_users_that_may_publish": publishers,
            "creator_may_publish": principal.can(PUBLISH_PERMISSION),
            "creator_may_approve": principal.can(APPROVE_PERMISSION),
        }

    # ------------------------------------------------------------ transitions
    def approve(self, principal: Principal, version_id: int) -> dict[str, Any]:
        self._require(principal, APPROVE_PERMISSION, "seismic.param_rehearsal", version_id)

        def apply(connection: sqlite3.Connection, version: sqlite3.Row) -> None:
            if version["status"] == "approved":
                self._guard_failure(connection, version, "approve", principal, "候选已确认，请勿重复确认")
            if version["status"] != "staged":
                self._guard_failure(connection, version, "approve", principal, f"候选处于 {version['status']} 状态，不能确认")
            now_text = to_storage(self.clock.now())
            connection.execute(
                "UPDATE seismic_param_versions SET status='approved',approved_by_user_id=?,approved_by_name=?,"
                "approved_at=?,updated_at=? WHERE id=?",
                (principal.user_id, principal.display_name, now_text, now_text, version_id),
            )
            self._stage_log(connection, version_id, "approve", principal, "success", {})
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="seismic.rehearsal.approve",
                resource_type="seismic_param_version",
                resource_id=version_id,
                before={"status": "staged"},
                after={"status": "approved", "version_tag": version["version_tag"]},
            )

        self._run_transition(version_id, principal, apply)
        return self.detail(version_id)

    def reject(self, principal: Principal, version_id: int, reason: str) -> dict[str, Any]:
        self._require(principal, APPROVE_PERMISSION, "seismic.param_rehearsal", version_id)

        def apply(connection: sqlite3.Connection, version: sqlite3.Row) -> None:
            if version["status"] in {"rejected", "active", "superseded"}:
                self._guard_failure(connection, version, "reject", principal, f"候选处于 {version['status']} 状态，不能驳回")
            now_text = to_storage(self.clock.now())
            connection.execute(
                "UPDATE seismic_param_versions SET status='rejected',rejected_by_user_id=?,rejected_by_name=?,"
                "rejected_at=?,reject_reason=?,updated_at=? WHERE id=?",
                (principal.user_id, principal.display_name, now_text, reason[:500], now_text, version_id),
            )
            self._stage_log(connection, version_id, "reject", principal, "success", {"reason": reason})
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="seismic.rehearsal.reject",
                resource_type="seismic_param_version",
                resource_id=version_id,
                before={"status": version["status"]},
                after={"status": "rejected", "version_tag": version["version_tag"], "reason": reason},
            )

        self._run_transition(version_id, principal, apply)
        return self.detail(version_id)

    def publish(self, principal: Principal, version_id: int, expected_digest: str | None = None) -> dict[str, Any]:
        self._require(principal, PUBLISH_PERMISSION, "seismic.param_rehearsal", version_id)

        def apply(connection: sqlite3.Connection, version: sqlite3.Row) -> None:
            if version["status"] == "active":
                self._guard_failure(connection, version, "publish", principal, "候选已经发布，请勿重复发布")
            if version["status"] == "superseded":
                self._guard_failure(connection, version, "publish", principal, "候选已被更新的生效版本取代")
            if version["status"] == "rejected":
                self._guard_failure(connection, version, "publish", principal, "候选已被驳回，不能发布")
            if version["status"] != "approved":
                self._guard_failure(connection, version, "publish", principal, "只有审批通过的候选才能发布")
            if expected_digest and expected_digest != version["params_digest"]:
                self._guard_failure(connection, version, "publish", principal, "提交的候选摘要与预演候选集不一致，已拒绝切换")
            stored_params = json.loads(version["params_json"])
            if parameters_digest(stored_params) != version["params_digest"]:
                self._guard_failure(connection, version, "publish", principal, "候选参数摘要校验失败，已拒绝切换")

            active = self._read_active()
            now_text = to_storage(self.clock.now())
            # 以下三条更新在同一笔 IMMEDIATE 事务内：要么整体生效，要么整体不发生。
            connection.execute(
                "UPDATE seismic_param_versions SET status='superseded',updated_at=? WHERE status='active'",
                (now_text,),
            )
            connection.execute(
                "UPDATE seismic_param_versions SET status='active',published_at=?,published_by_user_id=?,"
                "published_by_name=?,updated_at=? WHERE id=?",
                (now_text, principal.user_id, principal.display_name, now_text, version_id),
            )
            connection.execute(
                "UPDATE seismic_active_params SET version_tag=?,params_json=?,params_digest=?,"
                "activated_version_id=?,activated_by_user_id=?,activated_by_name=?,activated_at=? WHERE id=1",
                (
                    version["version_tag"],
                    version["params_json"],
                    version["params_digest"],
                    version_id,
                    principal.user_id,
                    principal.display_name,
                    now_text,
                ),
            )
            self._stage_log(connection, version_id, "publish", principal, "success", {"supersedes": active["version_tag"]})
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="seismic.rehearsal.publish",
                resource_type="seismic_param_version",
                resource_id=version_id,
                before={"active_version": active["version_tag"]},
                after={"active_version": version["version_tag"], "params": stored_params},
                metadata={"supersedes": active["version_tag"]},
            )

        self._run_transition(version_id, principal, apply)
        return self.detail(version_id)

    def _run_transition(
        self,
        version_id: int,
        principal: Principal,
        apply: Callable[[sqlite3.Connection, sqlite3.Row], None],
    ) -> None:
        connection = self.connection
        self.ensure_schema()
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = self._require_version(version_id)
            self._expire_if_due(connection, version, principal)
            apply(connection, version)
            connection.commit()
        except ConflictError:
            # 守卫失败与过期落库已自行提交；其余路径不应吞掉真实异常。
            if connection.in_transaction:
                connection.rollback()
            raise
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    # ------------------------------------------------------------- reads/logs
    def list_rehearsals(self, principal: Principal, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self._require(principal, READ_PERMISSION, "seismic.param_rehearsal", None)
        self.ensure_schema()
        if status:
            rows = self.connection.execute(
                "SELECT * FROM seismic_param_versions WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM seismic_param_versions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._summary(dict(item)) for item in rows]

    def detail(self, version_id: int, principal: Principal | None = None) -> dict[str, Any]:
        if principal is not None:
            self._require(principal, READ_PERMISSION, "seismic.param_rehearsal", version_id)
        self.ensure_schema()
        row = self._require_version(version_id)
        result = self._summary(dict(row))
        result["report"] = json.loads(row["report_json"]) if row["report_json"] else {}
        result["stage_log"] = [
            dict(item)
            for item in self.connection.execute(
                "SELECT stage,actor_user_id,actor_name,outcome,detail_json,created_at "
                "FROM seismic_param_stage_log WHERE version_id=? ORDER BY id",
                (version_id,),
            ).fetchall()
        ]
        return result

    def report_summary(self, principal: Principal, version_id: int) -> dict[str, Any]:
        self._require(principal, READ_PERMISSION, "seismic.param_rehearsal", version_id)
        self.ensure_schema()
        row = self._require_version(version_id)
        report = json.loads(row["report_json"]) if row["report_json"] else {}
        return {
            "version_id": version_id,
            "version_tag": row["version_tag"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            "candidate_digest": report.get("candidate_digest"),
            "input_diff": report.get("input_diff"),
            "impact_scope": report.get("impact_scope"),
            "permission_check": report.get("permission_check"),
            "generated_at": report.get("generated_at"),
        }

    def stage_log(self, version_id: int) -> list[dict[str, Any]]:
        self.ensure_schema()
        self._require_version(version_id)
        return [
            dict(item)
            for item in self.connection.execute(
                "SELECT * FROM seismic_param_stage_log WHERE version_id=? ORDER BY id", (version_id,)
            ).fetchall()
        ]

    # ------------------------------------------------------------- internals
    def _resolve_sample_events(self, event_ids: list[int] | None, limit: int) -> list[int]:
        if event_ids:
            found: list[int] = []
            for raw in event_ids[:MAX_SAMPLE_LIMIT]:
                row = self.connection.execute("SELECT id FROM seismic_events WHERE id=?", (raw,)).fetchone()
                if row is None:
                    raise NotFoundError(f"代表性事件不存在：{raw}")
                found.append(int(row["id"]))
            return list(dict.fromkeys(found))
        rows = self.connection.execute(
            "SELECT e.id FROM seismic_events e LEFT JOIN seismic_observations o ON o.event_id=e.id "
            "GROUP BY e.id ORDER BY COUNT(o.id) DESC, e.id LIMIT ?",
            (limit,),
        ).fetchall()
        return [int(item["id"]) for item in rows]

    def _merge_and_validate(self, candidate: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        unknown = set(candidate) - set(PARAMETER_FIELDS)
        if unknown:
            raise ValidationError(f"未知的参数字段：{sorted(unknown)}")
        params = dict(current)
        params.update(candidate)
        self._validate_ranges(params)
        return params

    @staticmethod
    def _validate_ranges(params: dict[str, Any]) -> None:
        if not isinstance(params["model_version"], str) or not 1 <= len(params["model_version"]) <= 40:
            raise ValidationError("model_version 长度必须在 1 到 40 之间")
        bounds = {
            "grid_step_km": (0.0, 100.0, False),
            "radius_km": (0.0, 1000.0, False),
            "pga_limit": (0.0, 100.0, False),
            "pgv_limit": (0.0, 500.0, False),
            "quality_accept_score": (0.0, 1.0, True),
            "pga_weight": (0.0, 1.0, True),
        }
        for field, (low, high, inclusive_low) in bounds.items():
            value = params[field]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValidationError(f"{field} 必须是数值")
            if value < low or (value == low and not inclusive_low) or value > high:
                raise ValidationError(f"{field} 超出允许范围")
        if params["grid_step_km"] > params["radius_km"]:
            raise ValidationError("grid_step_km 不能大于 radius_km")

    def _require_version(self, version_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM seismic_param_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("预演候选不存在")
        return row

    def _expire_if_due(
        self, connection: sqlite3.Connection, version: sqlite3.Row, principal: Principal
    ) -> None:
        if version["status"] not in {"staged", "approved"}:
            return
        expires = from_storage(version["expires_at"])
        if expires is None or expires > self.clock.now():
            return
        now_text = to_storage(self.clock.now())
        connection.execute(
            "UPDATE seismic_param_versions SET status='expired',updated_at=? WHERE id=?",
            (now_text, version["id"]),
        )
        self._stage_log(connection, version["id"], "expire", principal, "failure", {"reason": "候选超过有效期"})
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="seismic.rehearsal.expire",
            resource_type="seismic_param_version",
            resource_id=version["id"],
            outcome="failure",
            after={"status": "expired", "version_tag": version["version_tag"]},
        )
        # 过期标记必须留下审计痕迹，先提交再以冲突失败。
        connection.commit()
        raise ConflictError("候选参数集已过期，请重新发起预演")

    def _stage_log(
        self,
        connection: sqlite3.Connection,
        version_id: int,
        stage: str,
        principal: Principal,
        outcome: str,
        detail: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO seismic_param_stage_log(version_id,stage,actor_user_id,actor_name,outcome,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                version_id,
                stage,
                principal.user_id,
                principal.display_name,
                outcome,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                to_storage(self.clock.now()),
            ),
        )

    def _guard_failure(
        self,
        connection: sqlite3.Connection,
        version: sqlite3.Row,
        stage: str,
        principal: Principal,
        message: str,
    ) -> None:
        self._stage_log(connection, version["id"], stage, principal, "failure", {"reason": message})
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action=f"seismic.rehearsal.{stage}",
            resource_type="seismic_param_version",
            resource_id=version["id"],
            outcome="failure",
            metadata={"reason": message, "status": version["status"], "version_tag": version["version_tag"]},
        )
        # 失败尝试也要留痕：先提交日志再抛出冲突。
        connection.commit()
        raise ConflictError(message)

    def _require(self, principal: Principal, permission: str, resource_type: str, resource_id: Any) -> None:
        if principal.can(permission):
            return
        try:
            self.ensure_schema()
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action=f"seismic.rehearsal.{permission.split('.')[-1]}",
                resource_type=resource_type,
                resource_id=resource_id,
                outcome="denied",
                metadata={"required_permission": permission},
            )
        finally:
            raise PermissionDeniedError(f"缺少权限：{permission}")

    @staticmethod
    def _summary(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "version_tag": row["version_tag"],
            "status": row["status"],
            "params": json.loads(row["params_json"]),
            "params_digest": row["params_digest"],
            "sample_event_ids": json.loads(row["sample_event_ids"] or "[]"),
            "created_by_name": row["created_by_name"],
            "approved_by_name": row["approved_by_name"],
            "approved_at": row["approved_at"],
            "rejected_by_name": row["rejected_by_name"],
            "reject_reason": row["reject_reason"],
            "expires_at": row["expires_at"],
            "published_at": row["published_at"],
            "published_by_name": row["published_by_name"],
            "supersedes_version_tag": row["supersedes_version_tag"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
