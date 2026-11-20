"""属性字典服务：管理用户属性字段的元数据定义，并负责枚举字段版本升级。"""
from __future__ import annotations

from typing import Any

from app.enum_rules import (
    CODE_FIELD_VERSION,
    LEGACY_FIELD_VERSION,
    migration_outcome,
)
from app.store import store


def list_attrs() -> list[dict[str, Any]]:
    """列出所有属性定义。"""
    return store.rows("attr_dict")


def get_attr(attr_id: str) -> dict[str, Any] | None:
    """获取单个属性定义。"""
    return store.find_by_field("attr_dict", "attr_id", attr_id)


def create_attr(data: dict[str, Any]) -> dict[str, Any]:
    """创建属性定义。"""
    row = store.insert("attr_dict", data)
    return row


def update_attr(attr_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    """更新属性定义。"""
    attr = store.find_by_field("attr_dict", "attr_id", attr_id)
    if attr is None:
        return None
    return store.update("attr_dict", attr["id"], updates)


def delete_attr(attr_id: str) -> bool:
    """删除属性定义。"""
    attr = store.find_by_field("attr_dict", "attr_id", attr_id)
    if attr is None:
        return False
    return store.delete("attr_dict", attr["id"])


def get_attrs_by_source(source: str) -> list[dict[str, Any]]:
    """按数据来源筛选属性。"""
    return store.find_all_by_field("attr_dict", "source", source)


def _samples_for(attr_id: str) -> list[dict[str, Any]]:
    """取某属性的存量枚举样本，按样本编号稳定排序。"""
    rows = store.find_all_by_field("attr_enum_sample", "attr_id", attr_id)
    return sorted(rows, key=lambda row: str(row.get("sample_id", "")))


def build_migration_detail(attr_id: str, excluded_sample_ids: list[str] | None = None) -> dict[str, Any]:
    """生成迁移结果明细：逐条样本给出代码、状态和可复核的依据。

    明细只做判定，不修改属性定义，可重复生成用于核对。
    ``excluded_sample_ids`` 是运营显式判定“暂不迁移、留待人工处置”的样本，
    它们不会阻止升级，但仍会原样列在明细里留痕。
    """
    attr = get_attr(attr_id)
    if attr is None:
        raise ValueError(f"Attribute not found: {attr_id}")

    excluded = set(excluded_sample_ids or [])
    # 样本记录上也可以预先标注 excluded，表示运营已判定该条不随本次升级。
    excluded.update(
        str(sample.get("sample_id"))
        for sample in _samples_for(attr_id)
        if sample.get("excluded")
    )
    details: list[dict[str, Any]] = []
    counts = {
        "migrated": 0,
        "blocked_missing_source": 0,
        "ambiguous": 0,
        "unmapped": 0,
        "excluded": 0,
    }
    for sample in _samples_for(attr_id):
        outcome = migration_outcome(attr, sample)
        sample_id = sample.get("sample_id", "")
        status = "excluded" if sample_id in excluded else outcome["status"]
        counts[status] = counts.get(status, 0) + 1
        details.append({
            "sample_id": sample_id,
            "attr_id": attr_id,
            "user_id": sample.get("user_id", ""),
            "raw_value": sample.get("raw_value", ""),
            "source": str(sample.get("source", "") or ""),
            "status": status,
            "migrated": status == "migrated",
            "code": outcome["code"] if status == "migrated" else None,
            "reason": (
                "运营显式排除，留待人工处置" if status == "excluded" else outcome["reason"]
            ),
            "candidates": outcome["candidates"],
            "from_field_version": int(attr.get("field_version", LEGACY_FIELD_VERSION)),
        })

    blocking = [
        row for row in details if row["status"] in ("ambiguous", "unmapped")
    ]
    return {
        "attr_id": attr_id,
        "attr_name": attr.get("attr_name", ""),
        "from_field_version": int(attr.get("field_version", LEGACY_FIELD_VERSION)),
        "target_field_version": CODE_FIELD_VERSION,
        "details": details,
        "counts": counts,
        "excluded_sample_ids": sorted(excluded),
        "can_apply": not blocking,
        "blocking_reasons": [
            {"sample_id": row["sample_id"], "reason": row["reason"]} for row in blocking
        ],
        "unmigrated": [
            {"sample_id": row["sample_id"], "reason": row["reason"], "status": row["status"]}
            for row in details
            if row["status"] != "migrated"
        ],
    }


def apply_migration(attr_id: str, excluded_sample_ids: list[str] | None = None) -> dict[str, Any]:
    """按明细把枚举字段升级到第 2 版。

    迁移是原子的：只要存在同一来源多代码或无法映射、且未被显式排除的样本，
    整次升级不落库，避免字段定义升级了却留下解释不了的存量值。
    缺来源的样本永远不会被迁移，无论是否排除。
    """
    attr = get_attr(attr_id)
    if attr is None:
        raise ValueError(f"Attribute not found: {attr_id}")

    detail = build_migration_detail(attr_id, excluded_sample_ids)
    if not detail["can_apply"]:
        raise ValueError(
            "存在无法唯一解释的存量样本，迁移已中止："
            + "；".join(item["reason"] for item in detail["blocking_reasons"])
        )
    # 缺来源的记录不允许通过“排除”洗成可迁移，仍会留在明细里。
    detail["blocked_missing_source"] = [
        row["sample_id"] for row in detail["details"] if row["status"] == "blocked_missing_source"
    ]

    target_values = list(attr.get("target_enum_values") or [])
    if not target_values:
        target_values = sorted({
            item["code"] for item in detail["details"] if item["code"]
        })
    if not target_values:
        raise ValueError("缺少第 2 版枚举代码定义，迁移已中止")

    # 原样保留 legacy_enum_values 与 compat_map，旧条件和新代码都能回到同一解释。
    legacy_values = list(attr.get("legacy_enum_values") or attr.get("enum_values") or [])
    updated = store.update("attr_dict", attr["id"], {
        "legacy_enum_values": legacy_values,
        "enum_values": target_values,
        "field_version": CODE_FIELD_VERSION,
        "status": "已生效",
        "pending": False,
        "abnormal": False,
        "version": f"v{CODE_FIELD_VERSION}.0",
    })

    result = {
        **detail,
        "result": "applied",
        "legacy_enum_values": legacy_values,
        "compat_map": list(attr.get("compat_map") or []),
        "enum_values": target_values,
        "migrated_sample_ids": [
            row["sample_id"] for row in detail["details"] if row["migrated"]
        ],
        "unmigrated_sample_ids": [
            row["sample_id"] for row in detail["details"] if row["status"] != "migrated"
        ],
    }
    # 迁移明细落成跨模块产物，供标签计算和验证环境按属性回查。
    store.set_artifact("attr_migration", attr_id, result)
    return {**result, "attr": dict(updated or attr)}


def get_migration_detail(attr_id: str) -> dict[str, Any] | None:
    """获取已落库的迁移明细；未执行过迁移时按当前定义现算一份。"""
    artifact = store.get_artifact("attr_migration", attr_id)
    if artifact is not None:
        return artifact
    try:
        return build_migration_detail(attr_id)
    except ValueError:
        return None


def list_migrations() -> list[dict[str, Any]]:
    """列出所有已落库的迁移明细。"""
    return store.list_artifacts("attr_migration")
