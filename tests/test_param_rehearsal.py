from __future__ import annotations

from app.database import close_connection, get_connection
from app.seismic.rehearsal import ParameterRehearsalService


def _event(external_id: str = "EQ-RH-001") -> dict:
    return {
        "external_id": external_id,
        "origin_time": "2026-09-24T12:00:00+00:00",
        "latitude": 30.1,
        "longitude": 103.2,
        "depth_km": 12.0,
        "magnitude": 5.8,
        "magnitude_type": "ML",
        "source": "test",
    }


def _seed_event(client, external_id: str = "EQ-RH-001", published: bool = False) -> int:
    event_id = client.post("/api/seismic/events", json=_event(external_id)).json()["id"]
    # pga=15 在基线 pga_limit=20 下通过，候选 pga_limit=10 下会被拒。
    client.post(
        f"/api/seismic/events/{event_id}/observations",
        json={"station_code": "RH01", "channel": "HNZ", "observed_at": "2026-09-24T12:00:03+00:00", "pga": 15.0, "pgv": 2.1, "distance_km": 18},
    )
    if published:
        client.patch(f"/api/seismic/events/{event_id}", json={"status": "published"})
    return event_id


def _make_user(client, admin, username, role_code, permissions):
    client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": role_code, "name": role_code, "permission_codes": permissions},
    )
    client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Role!23456x", "display_name": username, "role_codes": [role_code]},
    )
    login = client.post("/api/auth/login", json={"username": username, "password": "Role!23456x", "client_label": "tests"})
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _rehearser(client, admin):
    return _make_user(client, admin, "rehearser", "rh.rehearser", ["seismic.params.read", "seismic.params.rehearse"])


def _approver(client, admin):
    return _make_user(client, admin, "approver", "rh.approver", ["seismic.params.read", "seismic.params.approve"])


def _publisher(client, admin):
    return _make_user(client, admin, "publisher", "rh.publisher", ["seismic.params.read", "seismic.params.publish"])


def _create(client, headers, event_id, candidate=None):
    return client.post(
        "/api/seismic/param-rehearsals",
        headers=headers,
        json={"candidate": candidate or {"pga_limit": 10.0}, "event_ids": [event_id], "ttl_minutes": 60, "reason": "收紧 PGA 量程"},
    )


def test_rehearsal_generates_full_report_without_touching_production(client, admin):
    event_id = _seed_event(client, published=True)
    active_before = client.get("/api/seismic/params/active", headers=admin["headers"]).json()
    assert active_before["version_tag"] == "baseline-gmpe-2026.1"

    created = _create(client, admin["headers"], event_id)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "staged"
    assert body["expires_at"] > body["created_at"]
    report = body["report"]
    assert report["input_diff"]["parameters"]["pga_limit"] == {"baseline": 20.0, "candidate": 10.0}
    event_report = report["events"][0]
    assert event_report["affected"] is True
    assert event_report["input_diff"]["baseline_accepted"] == 1
    assert event_report["input_diff"]["candidate_accepted"] == 0
    assert event_report["input_diff"]["flipped"][0]["after_status"] == "rejected"
    assert event_report["result_diff"]["changed_points"] > 0
    assert event_report["zone_summaries"]["baseline"] != event_report["zone_summaries"]["candidate"]
    impact = report["impact_scope"]
    assert impact["events_affected"] == 1
    assert impact["affected_event_ids"] == [event_id]
    assert impact["published_events_affected"] == ["EQ-RH-001"]
    permission = report["permission_check"]
    assert permission["required_permission"] == "seismic.params.publish"
    assert {role["code"] for role in permission["roles_that_may_publish"]} >= {"administrator"}

    # 生效版本与生产事件数据在预演后保持不变。
    active_after = client.get("/api/seismic/params/active", headers=admin["headers"]).json()
    assert active_after["params_digest"] == active_before["params_digest"]
    event = client.get(f"/api/seismic/events/{event_id}").json()
    assert event["observations"][0]["quality_status"] == "accepted"
    assert event["version"] == 2  # 只有 patch status 产生过一次版本递增


def test_approve_then_publish_atomically_switches_active_version(client, admin):
    event_id = _seed_event(client)
    version_id = _create(client, admin["headers"], event_id).json()["id"]

    approved = client.post(f"/api/seismic/param-rehearsals/{version_id}/approve", headers=admin["headers"])
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    # 发布前生效版本仍是基线。
    active = client.get("/api/seismic/params/active", headers=admin["headers"]).json()
    assert active["version_tag"] == "baseline-gmpe-2026.1"

    published = client.post(
        f"/api/seismic/param-rehearsals/{version_id}/publish", headers=admin["headers"], json={}
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "active"
    assert published.json()["published_by_name"] == "系统管理员"

    active = client.get("/api/seismic/params/active", headers=admin["headers"]).json()
    assert active["params"]["pga_limit"] == 10.0
    assert active["activated_version_id"] == version_id

    # 旧基线被标记为 superseded。
    versions = client.get("/api/seismic/param-rehearsals", headers=admin["headers"]).json()
    statuses = {item["version_tag"]: item["status"] for item in versions}
    assert statuses["baseline-gmpe-2026.1"] == "superseded"


def test_duplicate_approval_and_duplicate_publish_fail_safely(client, admin):
    event_id = _seed_event(client)
    version_id = _create(client, admin["headers"], event_id).json()["id"]
    client.post(f"/api/seismic/param-rehearsals/{version_id}/approve", headers=admin["headers"])

    again = client.post(f"/api/seismic/param-rehearsals/{version_id}/approve", headers=admin["headers"])
    assert again.status_code == 409
    detail = client.get(f"/api/seismic/param-rehearsals/{version_id}", headers=admin["headers"]).json()
    failures = [log for log in detail["stage_log"] if log["outcome"] == "failure"]
    assert any(log["stage"] == "approve" for log in failures)

    client.post(f"/api/seismic/param-rehearsals/{version_id}/publish", headers=admin["headers"], json={})
    republish = client.post(f"/api/seismic/param-rehearsals/{version_id}/publish", headers=admin["headers"], json={})
    assert republish.status_code == 409

    # 失败的重复操作进入审计。
    audit = client.get(
        "/api/audit?resource_type=seismic_param_version&outcome=failure&size=100", headers=admin["headers"]
    ).json()
    actions = {row["action"] for row in audit["data"]}
    assert "seismic.rehearsal.approve" in actions
    assert "seismic.rehearsal.publish" in actions


def test_expired_candidate_safe_fails_for_approval_and_publish(client, admin):
    event_id = _seed_event(client)
    version_id = _create(client, admin["headers"], event_id).json()["id"]
    # 模拟候选过期。
    connection = get_connection()
    connection.execute("UPDATE seismic_param_versions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (version_id,))

    expired_approve = client.post(f"/api/seismic/param-rehearsals/{version_id}/approve", headers=admin["headers"])
    assert expired_approve.status_code == 409
    detail = client.get(f"/api/seismic/param-rehearsals/{version_id}", headers=admin["headers"]).json()
    assert detail["status"] == "expired"

    expired_publish = client.post(
        f"/api/seismic/param-rehearsals/{version_id}/publish", headers=admin["headers"], json={}
    )
    assert expired_publish.status_code == 409
    active = client.get("/api/seismic/params/active", headers=admin["headers"]).json()
    assert active["version_tag"] == "baseline-gmpe-2026.1"


def test_publish_requires_approved_state_and_matching_digest(client, admin):
    event_id = _seed_event(client)
    version_id = _create(client, admin["headers"], event_id).json()["id"]

    # staged 不能直接发布。
    premature = client.post(
        f"/api/seismic/param-rehearsals/{version_id}/publish", headers=admin["headers"], json={}
    )
    assert premature.status_code == 409

    client.post(f"/api/seismic/param-rehearsals/{version_id}/approve", headers=admin["headers"])
    wrong_digest = client.post(
        f"/api/seismic/param-rehearsals/{version_id}/publish",
        headers=admin["headers"],
        json={"expected_digest": "deadbeef" * 8},
    )
    assert wrong_digest.status_code == 409
    active = client.get("/api/seismic/params/active", headers=admin["headers"]).json()
    assert active["version_tag"] == "baseline-gmpe-2026.1"


def test_rejected_candidate_cannot_be_published(client, admin):
    event_id = _seed_event(client)
    version_id = _create(client, admin["headers"], event_id).json()["id"]
    rejected = client.post(
        f"/api/seismic/param-rehearsals/{version_id}/reject", headers=admin["headers"], json={"reason": "影响已发布事件"}
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    # 即使管理员也不能发布被驳回的候选。
    blocked = client.post(
        f"/api/seismic/param-rehearsals/{version_id}/publish", headers=admin["headers"], json={}
    )
    assert blocked.status_code == 409


def test_role_separation_enforced(client, admin):
    event_id = _seed_event(client)
    rehearser = _rehearser(client, admin)
    approver = _approver(client, admin)
    publisher = _publisher(client, admin)

    # 预演员可以发起但不能审批/发布。
    created = _create(client, rehearser, event_id)
    assert created.status_code == 201, created.text
    version_id = created.json()["id"]
    assert client.post(f"/api/seismic/param-rehearsals/{version_id}/approve", headers=rehearser).status_code == 403
    assert client.post(f"/api/seismic/param-rehearsals/{version_id}/publish", headers=rehearser, json={}).status_code == 403

    # 审批员可以确认但不能发布。
    assert client.post(f"/api/seismic/param-rehearsals/{version_id}/approve", headers=approver).status_code == 200
    assert client.post(f"/api/seismic/param-rehearsals/{version_id}/publish", headers=approver, json={}).status_code == 403

    # 发布员可以发布同一候选集。
    published = client.post(f"/api/seismic/param-rehearsals/{version_id}/publish", headers=publisher, json={})
    assert published.status_code == 200, published.text

    # 无令牌请求被拒绝。
    assert client.get("/api/seismic/param-rehearsals").status_code == 401


def test_rehearsal_query_endpoints_and_report_summary(client, admin):
    event_id = _seed_event(client)
    version_id = _create(client, admin["headers"], event_id).json()["id"]

    listing = client.get("/api/seismic/param-rehearsals?status=staged", headers=admin["headers"])
    assert listing.status_code == 200
    assert any(item["id"] == version_id for item in listing.json())

    report = client.get(f"/api/seismic/param-rehearsals/{version_id}/report", headers=admin["headers"])
    assert report.status_code == 200
    body = report.json()
    assert body["impact_scope"]["events_evaluated"] == 1
    assert body["input_diff"]["parameters"]["pga_limit"]["candidate"] == 10.0
    assert "events" not in body  # 摘要不携带逐事件大对象


def test_unfinished_rehearsal_survives_restart_without_becoming_active(client, admin):
    event_id = _seed_event(client)
    version_id = _create(client, admin["headers"], event_id).json()["id"]

    # 模拟服务重启：关闭连接后以全新服务实例重新建表与读取。
    close_connection()
    service = ParameterRehearsalService(get_connection())
    service.ensure_schema()
    detail = service.detail(version_id)
    assert detail["status"] == "staged"
    assert service.active_version()["version_tag"] == "baseline-gmpe-2026.1"

    # 重启后预演仍可继续走完审批发布。
    from app.core.security import Principal

    principal = Principal(admin_user_id(client), "admin", "admin", None, frozenset({"*"}), session_id=1)
    service.approve(principal, version_id)
    service.publish(principal, version_id)
    assert service.active_version()["version_tag"] == detail["version_tag"]


def admin_user_id(client) -> int:
    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "Admin!23456", "client_label": "tests"}
    )
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    return client.get("/api/auth/me", headers=headers).json()["user_id"]


def test_published_version_governs_live_ingestion_and_computation(client, admin):
    # 事件 A 用于预演并驱动发布；事件 B 在发布之后才登记，其观测按新生效参数判定质量。
    event_a = _seed_event(client, "EQ-RH-A")
    version_id = _create(client, admin["headers"], event_a, candidate={"pga_limit": 10.0}).json()["id"]
    client.post(f"/api/seismic/param-rehearsals/{version_id}/approve", headers=admin["headers"])
    client.post(f"/api/seismic/param-rehearsals/{version_id}/publish", headers=admin["headers"], json={})

    event_b = client.post("/api/seismic/events", json={**_event("EQ-RH-B")}).json()["id"]
    observation = client.post(
        f"/api/seismic/events/{event_b}/observations",
        json={"station_code": "RH02", "channel": "HNZ", "observed_at": "2026-09-24T12:00:04+00:00", "pga": 15.0, "distance_km": 22},
    ).json()
    # pga=15 在新生效的 pga_limit=10 下必须被拒绝（基线下会通过）。
    assert observation["quality_status"] == "rejected"

    # 计算结果中记录的模型版本来自生效参数集。
    client.post(
        f"/api/seismic/events/{event_b}/computations",
        json={"model_version": "gmpe-2026.1", "grid_step_km": 20, "radius_km": 20, "requested_by": "test"},
    )
    claimed = client.post("/api/seismic/computations/claim?worker_id=w1").json()["task"]
    result = client.post(f"/api/seismic/computations/{claimed['id']}/calculate?worker_id=w1").json()
    assert result["status"] == "done"
    assert result["result_json"]  # 无 accepted 观测时回退到震级经验式，仍能正常产出


def test_candidate_equal_to_active_is_rejected(client, admin):
    _seed_event(client)
    response = client.post(
        "/api/seismic/param-rehearsals",
        headers=admin["headers"],
        json={"candidate": {"pga_limit": 20.0}, "sample_limit": 5},
    )
    assert response.status_code == 422


def test_every_stage_is_audited(client, admin):
    event_id = _seed_event(client)
    version_id = _create(client, admin["headers"], event_id).json()["id"]
    client.post(f"/api/seismic/param-rehearsals/{version_id}/approve", headers=admin["headers"])
    client.post(f"/api/seismic/param-rehearsals/{version_id}/publish", headers=admin["headers"], json={})

    audit = client.get(
        "/api/audit?resource_type=seismic_param_version&size=100", headers=admin["headers"]
    ).json()
    actions = [row["action"] for row in audit["data"]]
    for expected in ("seismic.rehearsal.create", "seismic.rehearsal.approve", "seismic.rehearsal.publish"):
        assert expected in actions
    publish_row = next(row for row in audit["data"] if row["action"] == "seismic.rehearsal.publish")
    assert publish_row["before_json"]
    assert publish_row["after_json"]
