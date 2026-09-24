"""烈度参数预演接口：发起预演、查询状态/报告、审批与原子发布。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection
from app.seismic.rehearsal import (
    APPROVE_PERMISSION,
    PUBLISH_PERMISSION,
    READ_PERMISSION,
    REHEARSE_PERMISSION,
    ParameterRehearsalService,
)
from app.seismic.schemas import RehearsalCreate, RehearsalDecision, RehearsalPublish

router = APIRouter(prefix="/api/seismic/param-rehearsals", tags=["烈度参数预演"])


def service() -> ParameterRehearsalService:
    # 变更类方法自行管理 IMMEDIATE 事务，保证发布与失败留痕的原子性。
    return ParameterRehearsalService(get_connection())


@router.post("", status_code=201)
def create_rehearsal(payload: RehearsalCreate, principal: Principal = Depends(current_principal)) -> dict:
    return service().create_rehearsal(
        principal,
        payload.candidate.changed_fields(),
        event_ids=payload.event_ids,
        sample_limit=payload.sample_limit,
        ttl_minutes=payload.ttl_minutes,
        reason=payload.reason,
    )


@router.get("")
def list_rehearsals(
    status: str | None = Query(default=None, pattern="^(staged|approved|rejected|active|superseded|expired)$"),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    return service().list_rehearsals(principal, status=status, limit=limit)


@router.get("/{version_id}")
def get_rehearsal(version_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().detail(version_id, principal)


@router.get("/{version_id}/report")
def get_report(version_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().report_summary(principal, version_id)


@router.post("/{version_id}/approve")
def approve_rehearsal(version_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().approve(principal, version_id)


@router.post("/{version_id}/reject")
def reject_rehearsal(
    version_id: int, payload: RehearsalDecision, principal: Principal = Depends(current_principal)
) -> dict:
    return service().reject(principal, version_id, payload.reason)


@router.post("/{version_id}/publish")
def publish_rehearsal(
    version_id: int, payload: RehearsalPublish, principal: Principal = Depends(current_principal)
) -> dict:
    return service().publish(principal, version_id, payload.expected_digest)


active_router = APIRouter(prefix="/api/seismic/params", tags=["烈度参数预演"])


@active_router.get("/active")
def active_params(principal: Principal = Depends(current_principal)) -> dict:
    # 任一参数治理权限都可查询最终发布版本。
    if not any(
        principal.can(code)
        for code in (READ_PERMISSION, REHEARSE_PERMISSION, APPROVE_PERMISSION, PUBLISH_PERMISSION)
    ):
        principal.require(READ_PERMISSION)
    return service().active_version()
