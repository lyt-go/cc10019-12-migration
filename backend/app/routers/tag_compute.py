"""标签计算路由：执行标签计算任务并获取结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.tag_compute import (
    ComputeSwitchError,
    compute_tag,
    get_compute_result,
    list_compute_results,
    list_run_snapshots,
    recompute_tag,
    replay_task,
    request_field_versions,
    switch_field_versions,
)
from app.store import store

router = APIRouter(prefix="/api/tag-compute", tags=["tag_compute"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("tag_compute")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}



@router.get("/runs")
def runs(task_id: str | None = None) -> dict[str, Any]:
    """计算运行快照：每次尝试实际启用的字段版本与状态。"""
    items = list_run_snapshots(task_id)
    return {"items": items, "total": len(items)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("tag_compute")
    return {"items": rows, "total": len(rows)}
@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("tag_compute", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{task_id}/run")
def run(task_id: str, payload: dict[str, Any] | None = None) -> ActionResult:
    """执行任务；可提交 ``field_versions``，先登记为目标口径再计算。"""
    requested = (payload or {}).get("field_versions") or {}
    try:
        if requested:
            result = switch_field_versions(task_id, requested)
        else:
            result = compute_tag(task_id)
    except ComputeSwitchError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "issues": exc.issues})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ActionResult(ok=True, message="Computed", entry=result.model_dump())


@router.get("/{task_id}/result")
def result(task_id: str) -> dict[str, Any]:
    res = get_compute_result(task_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{task_id}/field-versions")
def set_field_versions(task_id: str, payload: dict[str, Any]) -> ActionResult:
    """只登记目标字段版本，不触发计算。"""
    try:
        values = request_field_versions(task_id, payload.get("field_versions") or {})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ActionResult(ok=True, message="Requested", entry={"field_versions": values})


@router.get("/artifacts/all")
def artifacts() -> list[dict[str, Any]]:
    return list_compute_results()


@router.post("/{task_id}/replay")
def replay(task_id: str) -> ActionResult:
    """按任务当时记录的字段版本回查，不改变现有口径。"""
    try:
        result = replay_task(task_id)
    except ComputeSwitchError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "issues": exc.issues})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ActionResult(ok=True, message="Replayed", entry=result)


@router.post("/{task_id}/recompute")
def recompute(task_id: str, payload: dict[str, Any] | None = None) -> ActionResult:
    scope = payload.get("scope") if payload else None
    try:
        result = recompute_tag(task_id, scope)
        return ActionResult(ok=True, message="Recomputed", entry=result.model_dump())
    except ComputeSwitchError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "issues": exc.issues})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("tag_compute", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
