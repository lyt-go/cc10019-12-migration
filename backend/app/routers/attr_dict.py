"""属性字典路由：管理用户属性字段的元数据定义与枚举版本迁移。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.attr_dict import (
    apply_migration,
    build_migration_detail,
    get_attr,
    get_migration_detail,
    list_attrs,
    list_migrations,
    create_attr,
    update_attr,
    delete_attr,
    get_attrs_by_source,
)
from app.store import store, query_filters

router = APIRouter(prefix="/api/attr-dict", tags=["attr_dict"])


def _write_actions(payload: dict[str, Any]) -> dict[str, Any]:
    """动作载荷里的字段保留下来，便于页面按中文列名提交。"""
    return payload.get("values") if isinstance(payload.get("values"), dict) else payload


@router.get("")
def index(request: Request, page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("attr_dict")
    # query contract: filters=query_filters(request)
    filters = query_filters(request)
    if filters:
        rows = [r for r in rows if all(str(r.get(k)) == str(v) for k, v in filters.items())]
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}


@router.get("/migrations")
def migrations() -> dict[str, Any]:
    """列出已落库的迁移明细。"""
    items = list_migrations()
    return {"items": items, "total": len(items)}


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("attr_dict", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    row = dict(row)
    row["migration_detail"] = get_migration_detail(row["attr_id"])
    return row


@router.post("")
def create(payload: dict[str, Any]) -> ActionResult:
    row = create_attr(_write_actions(payload))
    return ActionResult(ok=True, message="Created", entry=row)


@router.put("/{entry_id}")
def update(entry_id: int, payload: dict[str, Any]) -> ActionResult:
    row = store.find("attr_dict", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    updated = update_attr(row["attr_id"], _write_actions(payload))
    return ActionResult(ok=True, message="Updated", entry=updated)


@router.delete("/{entry_id}")
def remove(entry_id: int) -> ActionResult:
    row = store.find("attr_dict", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    delete_attr(row["attr_id"])
    return ActionResult(ok=True, message="Deleted")


@router.get("/by-source/{source}")
def by_source(source: str) -> list[dict[str, Any]]:
    return get_attrs_by_source(source)


@router.get("/{attr_id}/migration")
def migration_detail(attr_id: str) -> dict[str, Any]:
    """生成或读取某个属性的迁移结果明细（不落库字段定义）。"""
    if get_attr(attr_id) is None:
        raise HTTPException(status_code=404, detail="Attribute not found")
    try:
        return build_migration_detail(attr_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{attr_id}/migration/apply")
def migrate(attr_id: str) -> ActionResult:
    """按明细把字段定义升级到第 2 版；存在无法解释的样本时整体中止。"""
    try:
        result = apply_migration(attr_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return ActionResult(ok=True, message="Migrated", entry=result)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("attr_dict", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
