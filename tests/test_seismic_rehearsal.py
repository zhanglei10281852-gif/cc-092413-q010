from __future__ import annotations

import json

from app.database import get_connection
from app.seismic.rehearsal import ParameterRehearsalService


def _event_payload(external_id: str, magnitude: float = 5.8) -> dict:
    return {
        "external_id": external_id,
        "origin_time": "2026-09-24T12:00:00+00:00",
        "latitude": 30.1,
        "longitude": 103.2,
        "depth_km": 12.0,
        "magnitude": magnitude,
        "magnitude_type": "ML",
        "source": "test",
    }


CANDIDATE = {
    "model_version": "gmpe-2026.2",
    "grid_step_km": 8.0,
    "radius_km": 100.0,
    "pga_weight": 0.02,
    "accepted_score_threshold": 0.6,
}


def _seed_event(client, external_id: str, *, status: str | None = None, compute: bool = False) -> int:
    event_id = client.post("/api/seismic/events", json=_event_payload(external_id)).json()["id"]
    client.post(
        f"/api/seismic/events/{event_id}/observations",
        json={"station_code": f"SC{event_id}A", "channel": "HNZ", "observed_at": "2026-09-24T12:00:03+00:00", "pga": 0.8, "pgv": 2.1, "distance_km": 18},
    )
    if status:
        client.patch(f"/api/seismic/events/{event_id}", json={"status": status})
    if compute:
        client.post(f"/api/seismic/events/{event_id}/computations", json={"requested_by": "test"})
        task_id = client.post("/api/seismic/computations/claim?worker_id=w1").json()["task"]["id"]
        client.post(f"/api/seismic/computations/{task_id}/calculate?worker_id=w1")
    return event_id


def _make_role_and_user(client, admin_headers: dict, username: str, permissions: list[str]) -> dict:
    role_code = f"role_{username}"
    role = client.post("/api/roles", headers=admin_headers, json={"code": role_code, "name": username, "permission_codes": permissions})
    assert role.status_code == 201, role.text
    password = "User!2345678"
    user = client.post(
        "/api/users",
        headers=admin_headers,
        json={"username": username, "password": password, "display_name": username, "role_codes": [role_code]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": password, "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def test_baseline_version_seeded_and_active_queryable(client, admin):
    response = client.get("/api/seismic/params/active", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["version_tag"] == "baseline-2026.1"
    assert body["params"]["pga_weight"] == 0.01
    versions = client.get("/api/seismic/params/versions", headers=admin["headers"])
    assert versions.status_code == 200
    assert len(versions.json()) == 1


def test_rehearsal_generates_full_report_without_changing_production(client, admin):
    event_id = _seed_event(client, "EQ-R-001", status="published", compute=True)
    before_active = client.get("/api/seismic/params/active", headers=admin["headers"]).json()

    created = client.post(
        "/api/seismic/param-rehearsals",
        headers=admin["headers"],
        json={"candidate": CANDIDATE, "event_ids": [event_id], "reason": "新衰减关系验证"},
    )
    assert created.status_code == 201, created.text
    rehearsal = created.json()
    assert rehearsal["status"] == "pending"
    assert rehearsal["event_ids"] == [event_id]

    report = rehearsal["report"]
    assert report["input_diff"]["baseline_version_tag"] == "baseline-2026.1"
    assert set(report["input_diff"]["changed_keys"]) == {"model_version", "grid_step_km", "pga_weight"}
    assert report["input_diff"]["changes"]["pga_weight"] == {"baseline": 0.01, "candidate": 0.02}
    event_report = report["events"][0]
    assert event_report["event_id"] == event_id
    assert event_report["points_total"] > 0
    assert event_report["points_changed"] > 0
    zones = event_report["zone_summary"]
    assert set(zones["baseline"]) == {"东北区", "东南区", "西北区", "西南区"}
    assert report["impact"]["affected_event_count"] == 1
    assert report["impact"]["published_event_affected_count"] == 1
    assert report["impact"]["stale_done_computation_count"] == 1
    permission = report["permission_check"]
    assert permission["can_approve"] is True
    assert permission["can_publish"] is True
    assert "administrator" in permission["approver_roles"]

    # 生产数据未变：生效版本不变、已完成任务结果不变
    after_active = client.get("/api/seismic/params/active", headers=admin["headers"]).json()
    assert after_active["fingerprint"] == before_active["fingerprint"]
    tasks = get_connection().execute("SELECT COUNT(*) AS c FROM seismic_computations").fetchone()["c"]
    assert tasks == 1

    # 状态与报告摘要可查询
    listed = client.get("/api/seismic/param-rehearsals", headers=admin["headers"])
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == rehearsal["id"]
    report_endpoint = client.get(f"/api/seismic/param-rehearsals/{rehearsal['id']}/report", headers=admin["headers"])
    assert report_endpoint.status_code == 200
    assert report_endpoint.json()["input_diff"]["candidate_fingerprint"] == rehearsal["candidate_fingerprint"]


def test_rehearsal_rejects_identical_and_invalid_candidate(client, admin):
    event_id = _seed_event(client, "EQ-R-002")
    active = client.get("/api/seismic/params/active", headers=admin["headers"]).json()["params"]
    same = client.post("/api/seismic/param-rehearsals", headers=admin["headers"], json={"candidate": active, "event_ids": [event_id]})
    assert same.status_code == 422
    bad = client.post(
        "/api/seismic/param-rehearsals",
        headers=admin["headers"],
        json={"candidate": {**CANDIDATE, "grid_step_km": 500}, "event_ids": [event_id]},
    )
    assert bad.status_code == 422
    missing = client.post(
        "/api/seismic/param-rehearsals",
        headers=admin["headers"],
        json={"candidate": {k: v for k, v in CANDIDATE.items() if k != "pga_weight"}, "event_ids": [event_id]},
    )
    assert missing.status_code == 422
    unknown_event = client.post(
        "/api/seismic/param-rehearsals",
        headers=admin["headers"],
        json={"candidate": CANDIDATE, "event_ids": [9999]},
    )
    assert unknown_event.status_code == 404


def test_approve_then_publish_atomically_switches_active_version(client, admin):
    event_id = _seed_event(client, "EQ-R-003")
    rehearsal_id = client.post(
        "/api/seismic/param-rehearsals", headers=admin["headers"], json={"candidate": CANDIDATE, "event_ids": [event_id]}
    ).json()["id"]

    # 未批准不能发布
    early = client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/publish", headers=admin["headers"])
    assert early.status_code == 409

    approved = client.post(
        f"/api/seismic/param-rehearsals/{rehearsal_id}/approve",
        headers=admin["headers"],
        json={"reason": "差异在可接受范围"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    # 重复审批安全失败
    again = client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/approve", headers=admin["headers"], json={})
    assert again.status_code == 409
    rejected_twice = client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/reject", headers=admin["headers"], json={})
    assert rejected_twice.status_code == 409

    published = client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/publish", headers=admin["headers"])
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    new_tag = published.json()["published_version_tag"]

    active = client.get("/api/seismic/params/active", headers=admin["headers"]).json()
    assert active["version_tag"] == new_tag
    assert active["params"]["pga_weight"] == 0.02
    versions = client.get("/api/seismic/params/versions", headers=admin["headers"]).json()
    assert {item["status"] for item in versions} == {"active", "retired"}
    assert sum(1 for item in versions if item["status"] == "active") == 1

    # 重复发布安全失败
    republish = client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/publish", headers=admin["headers"])
    assert republish.status_code == 409

    # 发布后新计算默认采用生效版本参数
    client.post(f"/api/seismic/events/{event_id}/observations", json={"station_code": "SC03B", "channel": "HNZ", "observed_at": "2026-09-24T12:00:09+00:00", "pga": 0.5, "distance_km": 40})
    client.post(f"/api/seismic/events/{event_id}/computations", json={"requested_by": "test"})
    task = client.post("/api/seismic/computations/claim?worker_id=w2").json()["task"]
    assert task["model_version"] == "gmpe-2026.2"
    assert task["grid_step_km"] == 8.0


def test_rejected_candidate_cannot_be_published(client, admin):
    event_id = _seed_event(client, "EQ-R-004")
    rehearsal_id = client.post(
        "/api/seismic/param-rehearsals", headers=admin["headers"], json={"candidate": CANDIDATE, "event_ids": [event_id]}
    ).json()["id"]
    rejected = client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/reject", headers=admin["headers"], json={"reason": "差异过大"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    publish = client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/publish", headers=admin["headers"])
    assert publish.status_code == 409
    active = client.get("/api/seismic/params/active", headers=admin["headers"]).json()
    assert active["version_tag"] == "baseline-2026.1"


def test_expired_candidate_safely_fails_approve_and_publish(client, admin):
    event_id = _seed_event(client, "EQ-R-005")
    rehearsal_id = client.post(
        "/api/seismic/param-rehearsals", headers=admin["headers"], json={"candidate": CANDIDATE, "event_ids": [event_id]}
    ).json()["id"]

    # 直接把过期时间拨到过去，模拟候选过期（无后台任务，重启不会自动发布）
    get_connection().execute(
        "UPDATE seismic_param_rehearsals SET expires_at=datetime('now','-1 minute') WHERE id=?", (rehearsal_id,)
    )

    approve = client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/approve", headers=admin["headers"], json={})
    assert approve.status_code == 409
    assert approve.json()["error"]["code"] == "conflict"
    detail = client.get(f"/api/seismic/param-rehearsals/{rehearsal_id}", headers=admin["headers"]).json()
    assert detail["status"] == "expired"
    publish = client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/publish", headers=admin["headers"])
    assert publish.status_code == 409
    active = client.get("/api/seismic/params/active", headers=admin["headers"]).json()
    assert active["version_tag"] == "baseline-2026.1"


def test_approved_rehearsal_expires_and_cannot_publish(client, admin):
    event_id = _seed_event(client, "EQ-R-006")
    rehearsal_id = client.post(
        "/api/seismic/param-rehearsals", headers=admin["headers"], json={"candidate": CANDIDATE, "event_ids": [event_id]}
    ).json()["id"]
    client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/approve", headers=admin["headers"], json={})
    get_connection().execute(
        "UPDATE seismic_param_rehearsals SET expires_at=datetime('now','-1 minute') WHERE id=?", (rehearsal_id,)
    )
    publish = client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/publish", headers=admin["headers"])
    assert publish.status_code == 409
    assert client.get(f"/api/seismic/param-rehearsals/{rehearsal_id}", headers=admin["headers"]).json()["status"] == "expired"


def test_permissions_enforced_per_stage(client, admin):
    event_id = _seed_event(client, "EQ-R-007")
    requester = _make_role_and_user(client, admin["headers"], "modelmgr", ["seismic.params.read", "seismic.params.rehearse"])
    approver = _make_role_and_user(client, admin["headers"], "approver1", ["seismic.params.read", "seismic.params.approve"])
    publisher = _make_role_and_user(client, admin["headers"], "publisher1", ["seismic.params.read", "seismic.params.publish"])
    nobody = _make_role_and_user(client, admin["headers"], "plainuser", [])

    # 无权限读取被拒
    denied_read = client.get("/api/seismic/params/active", headers=nobody)
    assert denied_read.status_code == 403

    # 模型管理员可以发起预演
    created = client.post("/api/seismic/param-rehearsals", headers=requester, json={"candidate": CANDIDATE, "event_ids": [event_id]})
    assert created.status_code == 201, created.text
    rehearsal_id = created.json()["id"]

    # 发起人不能自己审批/发布
    assert client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/approve", headers=requester, json={}).status_code == 403
    assert client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/publish", headers=requester).status_code == 403
    # 审批人不能发布
    assert client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/publish", headers=approver).status_code == 403
    # 发布人不能审批
    assert client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/approve", headers=publisher, json={}).status_code == 403

    assert client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/approve", headers=approver, json={"reason": "ok"}).status_code == 200
    assert client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/publish", headers=publisher).status_code == 200
    active = client.get("/api/seismic/params/active", headers=requester).json()
    assert active["params"]["model_version"] == "gmpe-2026.2"


def test_stale_baseline_after_another_publish_blocks_publish(client, admin):
    event_id = _seed_event(client, "EQ-R-008")
    first_candidate = {**CANDIDATE, "pga_weight": 0.015}
    first_id = client.post(
        "/api/seismic/param-rehearsals", headers=admin["headers"], json={"candidate": first_candidate, "event_ids": [event_id]}
    ).json()["id"]
    second_id = client.post(
        "/api/seismic/param-rehearsals", headers=admin["headers"], json={"candidate": CANDIDATE, "event_ids": [event_id]}
    ).json()["id"]
    client.post(f"/api/seismic/param-rehearsals/{first_id}/approve", headers=admin["headers"], json={})
    client.post(f"/api/seismic/param-rehearsals/{first_id}/publish", headers=admin["headers"])

    # 第二个预演基于旧基线批准后，发布必须安全失败
    client.post(f"/api/seismic/param-rehearsals/{second_id}/approve", headers=admin["headers"], json={})
    blocked = client.post(f"/api/seismic/param-rehearsals/{second_id}/publish", headers=admin["headers"])
    assert blocked.status_code == 409
    assert "基线" in blocked.json()["error"]["message"]


def test_all_stages_write_audit_events(client, admin):
    event_id = _seed_event(client, "EQ-R-009")
    rehearsal_id = client.post(
        "/api/seismic/param-rehearsals", headers=admin["headers"], json={"candidate": CANDIDATE, "event_ids": [event_id]}
    ).json()["id"]
    client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/approve", headers=admin["headers"], json={"reason": "ok"})
    client.post(f"/api/seismic/param-rehearsals/{rehearsal_id}/publish", headers=admin["headers"])

    events = client.get("/api/audit?resource_type=seismic_param_rehearsal&size=50", headers=admin["headers"]).json()["data"]
    actions = sorted(item["action"] for item in events)
    assert "seismic.rehearsal.approve" in actions
    assert "seismic.rehearsal.create" in actions
    publish_audit = client.get("/api/audit?resource_type=seismic_param_version&size=10", headers=admin["headers"]).json()["data"]
    assert any(item["action"] == "seismic.rehearsal.publish" for item in publish_audit)
    published_event = publish_audit[0]
    before = json.loads(published_event["before_json"])
    after = json.loads(published_event["after_json"])
    assert before["version_tag"] == "baseline-2026.1"
    assert after["rehearsal_id"] == rehearsal_id


def test_rehearsal_without_events_is_validation_error(client, admin):
    response = client.post("/api/seismic/param-rehearsals", headers=admin["headers"], json={"candidate": CANDIDATE})
    assert response.status_code == 422


def test_unfinished_rehearsal_survives_restart_and_never_auto_publishes(client, admin):
    event_id = _seed_event(client, "EQ-R-010")
    rehearsal_id = client.post(
        "/api/seismic/param-rehearsals", headers=admin["headers"], json={"candidate": CANDIDATE, "event_ids": [event_id]}
    ).json()["id"]

    # 模拟服务重启：释放线程连接后用新服务实例读取
    from app.database import close_connection

    close_connection()
    service = ParameterRehearsalService(get_connection())
    detail = service.get_rehearsal(rehearsal_id)
    assert detail["status"] == "pending"
    active = service.active_version()
    assert active["version_tag"] == "baseline-2026.1"
