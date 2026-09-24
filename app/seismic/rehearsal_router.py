from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.seismic.rehearsal import ParameterRehearsalService
from app.seismic.rehearsal_schemas import RehearsalCreate, RehearsalDecision

router = APIRouter(prefix="/api/seismic", tags=["烈度参数预演发布"])


def service() -> ParameterRehearsalService:
    return ParameterRehearsalService()


def _require_read(principal: Principal) -> None:
    if not (
        principal.can("seismic.params.read")
        or principal.can("seismic.params.rehearse")
        or principal.can("seismic.params.approve")
        or principal.can("seismic.params.publish")
    ):
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError("缺少权限：seismic.params.read")


@router.get("/params/active")
def active_params(principal: Principal = Depends(current_principal)) -> dict:
    _require_read(principal)
    return service().active_version()


@router.get("/params/versions")
def list_versions(principal: Principal = Depends(current_principal)) -> list[dict]:
    _require_read(principal)
    return service().list_versions()


@router.post("/param-rehearsals", status_code=201)
def create_rehearsal(payload: RehearsalCreate, principal: Principal = Depends(current_principal)) -> dict:
    return service().create_rehearsal(
        principal,
        payload.candidate.model_dump(),
        payload.event_ids,
        payload.reason,
    )


@router.get("/param-rehearsals")
def list_rehearsals(principal: Principal = Depends(current_principal)) -> list[dict]:
    _require_read(principal)
    return service().list_rehearsals()


@router.get("/param-rehearsals/{rehearsal_id}")
def get_rehearsal(rehearsal_id: int, principal: Principal = Depends(current_principal)) -> dict:
    _require_read(principal)
    return service().get_rehearsal(rehearsal_id)


@router.get("/param-rehearsals/{rehearsal_id}/report")
def get_report(rehearsal_id: int, principal: Principal = Depends(current_principal)) -> dict:
    _require_read(principal)
    return service().report(rehearsal_id)


@router.post("/param-rehearsals/{rehearsal_id}/approve")
def approve_rehearsal(rehearsal_id: int, payload: RehearsalDecision, principal: Principal = Depends(current_principal)) -> dict:
    return service().decide(principal, rehearsal_id, "approve", payload.reason)


@router.post("/param-rehearsals/{rehearsal_id}/reject")
def reject_rehearsal(rehearsal_id: int, payload: RehearsalDecision, principal: Principal = Depends(current_principal)) -> dict:
    return service().decide(principal, rehearsal_id, "reject", payload.reason)


@router.post("/param-rehearsals/{rehearsal_id}/publish")
def publish_rehearsal(rehearsal_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().publish(principal, rehearsal_id)
