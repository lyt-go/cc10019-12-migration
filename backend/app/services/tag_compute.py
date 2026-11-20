"""标签计算引擎：执行标签规则的批量计算，支持全量/增量/差量计算。

计算口径与字段版本绑定：
- 任务首次执行前可提交目标字段版本（``requested_field_versions``）；
- 每次计算把实际启用的字段版本写入任务、结果产物和运行快照；
- 已有成功口径的任务再计算时按当时记录的字段版本回查，不跟随字段定义漂移；
- 条件切换是原子的：任一属性无法唯一解析就整轮失败，不产出新旧混合的结果。
"""
from __future__ import annotations

import time
from typing import Any

from app.enum_rules import (
    CODE_FIELD_VERSION,
    ConditionSwitchError,
    LEGACY_FIELD_VERSION,
    migration_outcome,
    normalize_source,
    parse_condition_literals,
    resolve_condition,
)
from app.services.attr_dict import get_migration_detail
from app.store import store
from app.schemas import TagComputeResult

# 模拟用户属性值的采集来源；空串代表来源缺失，第 2 版计算无法解释该值。
_USER_SOURCES = ["event", "import", "derived", ""]


class ComputeSwitchError(ValueError):
    """条件切换失败：本轮没有生效的字段版本，也没有结果产物。"""

    def __init__(self, task_id: str, issues: list[dict[str, Any]]) -> None:
        self.task_id = task_id
        self.issues = issues
        super().__init__(
            f"任务 {task_id} 条件切换失败：" + "；".join(str(i.get("reason", "")) for i in issues)
        )


def _eval_condition(user: dict[str, Any], logic: str, attr_defs: dict[str, dict[str, Any]]) -> bool:
    """简化条件求值：支持 AND/OR/NOT 和基本比较。"""
    logic = logic.strip()
    # 处理括号
    if logic.startswith("(") and logic.endswith(")"):
        return _eval_condition(user, logic[1:-1], attr_defs)

    # 处理 NOT
    if logic.upper().startswith("NOT "):
        return not _eval_condition(user, logic[4:], attr_defs)

    # 处理 AND
    if " AND " in logic.upper():
        parts = _split_by_op(logic, "AND")
        return all(_eval_condition(user, p, attr_defs) for p in parts)

    # 处理 OR
    if " OR " in logic.upper():
        parts = _split_by_op(logic, "OR")
        return any(_eval_condition(user, p, attr_defs) for p in parts)

    # 基本比较：field op value
    return _eval_atom(user, logic, attr_defs)


def _split_by_op(logic: str, op: str) -> list[str]:
    """按操作符分割，注意括号嵌套。"""
    parts: list[str] = []
    depth = 0
    current = ""
    tokens = logic.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        if tok.upper() == op and depth == 0:
            if current.strip():
                parts.append(current.strip())
            current = ""
        else:
            current += tok + " "
        i += 1
    if current.strip():
        parts.append(current.strip())
    return parts if len(parts) > 1 else [logic]


def _eval_atom(user: dict[str, Any], logic: str, attr_defs: dict[str, Any]) -> bool:
    """求值原子条件。"""
    logic = logic.strip()
    # 支持 =, !=, <, >, <=, >=, IN, NOT IN
    for op in ["!=", "<=", ">=", "=", "<", ">"]:
        if op in logic:
            parts = logic.split(op, 1)
            if len(parts) == 2:
                left = parts[0].strip()
                right = parts[1].strip()
                left_val = user.get(left)
                # 处理字符串引号
                if right.startswith("'") and right.endswith("'"):
                    right = right[1:-1]
                elif right.startswith('"') and right.endswith('"'):
                    right = right[1:-1]
                # 处理数值
                try:
                    right = float(right)
                    if left_val is not None:
                        try:
                            left_val = float(left_val)
                        except ValueError:
                            pass
                except ValueError:
                    pass
                # 处理布尔
                if right == "True" or right == "true":
                    right = True
                elif right == "False" or right == "false":
                    right = False

                if op == "=":
                    return left_val == right
                elif op == "!=":
                    return left_val != right
                elif op == "<":
                    if left_val is None:
                        return False
                    return left_val < right
                elif op == ">":
                    if left_val is None:
                        return False
                    return left_val > right
                elif op == "<=":
                    if left_val is None:
                        return False
                    return left_val <= right
                elif op == ">=":
                    if left_val is None:
                        return False
                    return left_val >= right
    # 处理 IN
    if " IN " in logic.upper():
        parts = logic.upper().split(" IN ", 1)
        field = logic[:len(parts[0])].strip()
        val_list_str = logic[len(parts[0])+4:].strip()
        if val_list_str.startswith("[") and val_list_str.endswith("]"):
            val_list_str = val_list_str[1:-1]
        vals = [v.strip().strip("'\"").strip() for v in val_list_str.split(",")]
        field_val = str(user.get(field, ""))
        return field_val in vals

    return bool(user.get(logic, False))


def _attr_defs() -> dict[str, dict[str, Any]]:
    """属性定义，字段版本优先取已落库的迁移明细。"""
    defs: dict[str, dict[str, Any]] = {}
    for attr in store.rows("attr_dict"):
        attr_id = attr["attr_id"]
        definition = dict(attr)
        detail = get_migration_detail(attr_id)
        if detail and detail.get("result") == "applied":
            definition["field_version"] = detail.get("target_field_version", definition.get("field_version"))
            definition["compat_map"] = detail.get("compat_map", definition.get("compat_map", []))
            definition["legacy_enum_values"] = detail.get(
                "legacy_enum_values", definition.get("legacy_enum_values", [])
            )
        defs[attr_id] = definition
    return defs


def _referenced_attrs(tag: dict[str, Any], tag_defs: dict[str, dict[str, Any]], seen: set[str] | None = None) -> list[str]:
    """标签条件直接或经子标签间接引用的属性编号。"""
    seen = seen or set()
    tag_id = str(tag.get("tag_id", ""))
    if tag_id in seen:
        return []
    seen.add(tag_id)

    attrs: list[str] = []
    for attr_id in parse_condition_literals(tag.get("compute_logic", "")):
        attrs.append(attr_id)
    for ref in tag.get("source_attrs", []):
        if ref in tag_defs:
            attrs.extend(_referenced_attrs(tag_defs[ref], tag_defs, seen))
        elif ref.startswith("ATTR-"):
            attrs.append(ref)
    return sorted(set(attrs))


def _collect_tag_chain(tag_id: str, tag_defs: dict[str, dict[str, Any]], seen: set[str] | None = None) -> list[str]:
    """目标标签及其条件里引用的子标签，按依赖顺序返回。"""
    seen = seen or set()
    if tag_id in seen or tag_id not in tag_defs:
        return []
    seen.add(tag_id)
    order: list[str] = []
    for ref in tag_defs[tag_id].get("source_attrs", []):
        order.extend(_collect_tag_chain(ref, tag_defs, seen))
    order.append(tag_id)
    return order


def _resolve_tag_values(
    user: dict[str, Any],
    tag_id: str,
    tag_defs: dict[str, dict[str, Any]],
    logic_by_tag: dict[str, str],
    attr_defs: dict[str, dict[str, Any]],
    stack: set[str] | None = None,
) -> bool:
    """求值标签条件，并把被引用的子标签布尔值挂到用户记录上。"""
    stack = stack or set()
    if tag_id in stack or tag_id not in tag_defs:
        return False
    stack.add(tag_id)
    for ref in tag_defs[tag_id].get("source_attrs", []):
        if ref in tag_defs and ref not in user:
            user[ref] = _resolve_tag_values(user, ref, tag_defs, logic_by_tag, attr_defs, stack)
    stack.discard(tag_id)
    logic = logic_by_tag.get(tag_id, tag_defs[tag_id].get("compute_logic", ""))
    return _eval_condition(user, logic, attr_defs)


def _target_versions(
    task: dict[str, Any],
    attr_defs: dict[str, dict[str, Any]],
    tag: dict[str, Any],
    tag_defs: dict[str, dict[str, Any]],
    force_replay: bool = False,
) -> tuple[dict[str, int], str, dict[str, int]]:
    """确定本轮计算的字段版本。

    返回 (实际要用的版本, 模式, 提交的目标版本)：
    - ``force_replay`` 或任务已有生效版本 = 回查轮，完全按当时口径；
    - 显式提交了目标版本 = 切换轮，按提交值切换；
    - 两者都没有 = 首轮，按字段定义当前启用的版本。
    """
    recorded = {str(k): int(v) for k, v in (task.get("field_versions") or {}).items()}
    requested = {str(k): int(v) for k, v in (task.get("requested_field_versions") or {}).items()}
    used_attrs = _referenced_attrs(tag, tag_defs)

    effective: dict[str, int] = {}
    for attr_id in used_attrs:
        attr = attr_defs.get(attr_id)
        current = int(attr.get("field_version", LEGACY_FIELD_VERSION)) if attr else LEGACY_FIELD_VERSION
        if force_replay and recorded:
            effective[attr_id] = recorded.get(attr_id, current)
        elif requested:
            effective[attr_id] = requested.get(attr_id, current)
        elif recorded:
            effective[attr_id] = recorded.get(attr_id, current)
        else:
            effective[attr_id] = current

    if force_replay and recorded:
        return effective, "replay", {}
    if requested:
        return effective, "switch", requested
    if recorded:
        return effective, "replay", requested
    return effective, "upgrade", requested


def _check_ready(
    versions: dict[str, int], attr_defs: dict[str, dict[str, Any]], requested: dict[str, int]
) -> list[dict[str, Any]]:
    """目标版本必须是字段定义已启用的版本，否则不能在数据上计算新代码。"""
    issues: list[dict[str, Any]] = []
    for attr_id, version in versions.items():
        attr = attr_defs.get(attr_id)
        if attr is None:
            issues.append({
                "attr_id": attr_id,
                "code": "unknown_attribute",
                "reason": f"条件引用了未定义的属性 {attr_id}",
            })
            continue
        current = int(attr.get("field_version", LEGACY_FIELD_VERSION))
        if attr.get("data_type") == "enum" and version > current:
            issues.append({
                "attr_id": attr_id,
                "code": "field_version_not_ready",
                "requested_field_version": version,
                "current_field_version": current,
                "reason": f"属性 {attr_id} 尚未迁移到第 {version} 版，条件不能按新版代码计算",
            })
    return issues


def compute_tag(task_id: str, interrupt: bool = False, replay: bool = False) -> TagComputeResult:
    """执行标签计算任务。

    ``replay`` 强制按任务已记录的字段版本回查，不受待切换请求影响。
    ``interrupt`` 仅用于演练切换中断：在写入任何结果前中止，验证重算不会混用口径。
    """
    task_row = store.find_by_field("tag_compute", "task_id", task_id)
    if task_row is None:
        raise ValueError(f"Task not found: {task_id}")

    tag_id = task_row["tag_id"]
    tag = store.find_by_field("tag_define", "tag_id", tag_id)
    if tag is None:
        raise ValueError(f"Tag not found: {tag_id}")

    start_time = time.time()
    scope = task_row["compute_scope"]
    attr_defs = _attr_defs()
    tag_defs = {row["tag_id"]: row for row in store.rows("tag_define")}
    versions, mode, requested = _target_versions(task_row, attr_defs, tag, tag_defs, force_replay=replay)
    condition_snapshot = task_row.get("condition_snapshot") or tag["compute_logic"]
    declared_sources = dict(tag.get("condition_sources") or task_row.get("condition_sources") or {})
    condition_round = int(task_row.get("compute_round", 0)) + 1

    try:
        issues = _check_ready(versions, attr_defs, requested)
        if issues:
            raise ConditionSwitchError(issues)
        # 条件切换必须整体成功：目标标签和被引用的子标签一起解析，任一失败整轮中止。
        chain = _collect_tag_chain(tag_id, tag_defs)
        logic_by_tag: dict[str, str] = {}
        resolutions: list[dict[str, Any]] = []
        # 条件引用的每个字段都算“实际启用”，即使它的字面量无需转换。
        field_versions_used: dict[str, int] = dict(versions)
        for chain_tag_id in chain:
            chain_tag = tag_defs[chain_tag_id]
            chain_logic = (
                condition_snapshot if chain_tag_id == tag_id else chain_tag.get("compute_logic", "")
            )
            chain_sources = dict(chain_tag.get("condition_sources") or {})
            resolved_tag = resolve_condition(chain_logic, attr_defs, chain_sources, versions)
            logic_by_tag[chain_tag_id] = resolved_tag["logic"]
            field_versions_used.update(resolved_tag["field_versions"])
            for item in resolved_tag["resolutions"]:
                resolutions.append({**item, "tag_id": chain_tag_id})
    except ConditionSwitchError as exc:
        return _fail_round(task_row, condition_round, mode, requested, exc.issues)

    if interrupt:
        return _fail_round(
            task_row,
            condition_round,
            mode,
            requested,
            [{
                "code": "switch_interrupted",
                "reason": "切换过程被中断，本轮未产出结果",
            }],
        )

    users = _load_users_for_tag(attr_defs, scope, versions)
    used_attrs = sorted(field_versions_used)
    enum_attrs = [a for a in used_attrs if attr_defs.get(a, {}).get("data_type") == "enum"]
    unmapped_users = sum(
        1 for user in users if any(user.get(attr_id) is None for attr_id in enum_attrs)
    )

    results: list[dict[str, Any]] = []
    for user in users:
        matched = _resolve_tag_values(user, tag_id, tag_defs, logic_by_tag, attr_defs)
        results.append({
            "user_id": user.get("user_id"),
            "tag_id": tag_id,
            "tag_value": matched,
            "compute_time": task_row.get("output_partition", ""),
        })

    matched_ids = [row["user_id"] for row in results if row["tag_value"]]
    result_data = {
        "task_id": task_id,
        "tag_id": tag_id,
        "compute_scope": scope,
        "result_count": len(results),
        "matched_count": len(matched_ids),
        "matched_user_ids": matched_ids,
        "user_tag_values": results[:100],
        "output_partition": task_row.get("output_partition", ""),
        "compute_time_ms": int((time.time() - start_time) * 1000),
        "compute_round": condition_round,
        "mode": mode,
        "condition_snapshot": condition_snapshot,
        "condition_used": logic_by_tag.get(tag_id, ""),
        "requested_field_versions": requested,
        "field_versions": field_versions_used,
        "resolutions": resolutions,
        "declared_sources": declared_sources,
        "unmapped_user_count": unmapped_users,
        "switch_issues": [],
    }
    store.set_artifact("tag_compute", task_id, result_data)

    # 每次计算都留一条运行快照，记录实际启用的字段版本，便于事后核对。
    store.add_snapshot("tag_compute_run", {
        "task_id": task_id,
        "tag_id": tag_id,
        "compute_round": condition_round,
        "mode": mode,
        "exec_state": "success",
        "field_versions": field_versions_used,
        "requested_field_versions": requested,
        "condition_used": logic_by_tag.get(tag_id, ""),
    })

    store.update("tag_compute", task_row["id"], {
        "status": "已完成",
        "pending": False,
        "abnormal": False,
        "exec_state": "success",
        "result_count": len(results),
        "error_log": "",
        "compute_round": condition_round,
        "field_versions": field_versions_used,
        "requested_field_versions": {},
        "attempted_field_versions": requested,
        "condition_snapshot": condition_snapshot,
        "condition_sources": declared_sources,
        "resolved_condition": logic_by_tag.get(tag_id, ""),
        "mode": mode,
        "switch_issues": [],
    })

    return TagComputeResult(**result_data)


def _fail_round(
    task_row: dict[str, Any],
    condition_round: int,
    mode: str,
    requested: dict[str, int],
    issues: list[dict[str, Any]],
) -> TagComputeResult:
    """记录一次失败尝试：不写结果产物，也不覆盖已生效的字段版本。"""
    previous = dict(task_row.get("field_versions") or {})
    store.add_snapshot("tag_compute_run", {
        "task_id": task_row["task_id"],
        "tag_id": task_row["tag_id"],
        "compute_round": condition_round,
        "mode": mode,
        "exec_state": "failed",
        "field_versions": previous,
        "requested_field_versions": requested,
        "switch_issues": issues,
    })
    store.update("tag_compute", task_row["id"], {
        "status": "失败",
        "pending": True,
        "abnormal": True,
        "exec_state": "failed",
        "result_count": 0,
        "error_log": "；".join(str(item.get("reason", "")) for item in issues),
        "compute_round": condition_round,
        "attempted_field_versions": requested,
        "field_versions": previous,
        "mode": mode,
        "switch_issues": issues,
    })
    raise ComputeSwitchError(task_row["task_id"], issues)


def _load_users_for_tag(
    attr_defs: dict[str, dict[str, Any]],
    scope: str,
    field_versions: dict[str, int],
) -> list[dict[str, Any]]:
    """加载需要计算标签的用户数据，并按目标字段版本转换枚举值。

    样本里每种采集来源都有覆盖，缺来源的用户在第 2 版口径下保持未知并单独计数，
    不会被当成某个代码参与命中。
    """
    users = []
    for i in range(1, 126):
        user: dict[str, Any] = {
            "user_id": f"U{i:05d}",
            "ATTR-001": ["app", "web", "miniapp", "h5"][i % 4],
            "ATTR-002": i % 60,
            "ATTR-003": float(i * 15.5),
            "ATTR-004": ["ios", "android", "pc", "other"][i % 4],
            "ATTR-005": ["普通", "银卡", "金卡", "钻石"][i % 4],
        }
        # 增量模式只处理新用户
        if scope == "incremental" and i > 32:
            continue
        # 差量模式只处理变化用户
        if scope == "delta" and i % 3 != 0:
            continue
        user["_attr_sources"] = {"ATTR-005": _USER_SOURCES[i % len(_USER_SOURCES)]}
        users.append(user)

    # 第 2 版枚举字段：按用户自己的采集来源换成代码；缺来源的值保持未知。
    for attr_id, version in field_versions.items():
        attr = attr_defs.get(attr_id)
        if attr is None or attr.get("data_type") != "enum" or version < CODE_FIELD_VERSION:
            continue
        for user in users:
            source = normalize_source(user.get("_attr_sources", {}).get(attr_id, ""))
            outcome = migration_outcome(attr, {
                "raw_value": user.get(attr_id),
                "source": source,
            })
            user[attr_id] = outcome["code"] if outcome["status"] == "migrated" else None
    return users


def get_compute_result(task_id: str) -> dict[str, Any] | None:
    """获取标签计算结果。"""
    artifact = store.get_artifact("tag_compute", task_id)
    if artifact:
        return artifact
    # 尝试实时计算
    try:
        result = compute_tag(task_id)
        return result.model_dump()
    except ValueError:
        return None


def list_compute_results() -> list[dict[str, Any]]:
    """列出所有标签计算结果。"""
    return store.list_artifacts("tag_compute")


def list_run_snapshots(task_id: str | None = None) -> list[dict[str, Any]]:
    """列出计算运行快照，可按任务过滤。"""
    snapshots = store.list_snapshots("tag_compute_run")
    if task_id is None:
        return snapshots
    return [row for row in snapshots if row.get("task_id") == task_id]


def replay_task(task_id: str) -> dict[str, Any]:
    """按任务当时记录的字段版本回查，不改变任务的现有口径。"""
    result = compute_tag(task_id, replay=True)
    return result.model_dump()


def request_field_versions(task_id: str, field_versions: dict[str, Any]) -> dict[str, int]:
    """登记任务本轮要计算的目标字段版本。

    支持 ``{"*": 2}`` 表示标签条件引用的所有枚举字段都切到第 2 版。
    只登记“要切换到什么”，实际启用版本以成功那一轮的计算产物为准。
    """
    task_row = store.find_by_field("tag_compute", "task_id", task_id)
    if task_row is None:
        raise ValueError(f"Task not found: {task_id}")

    tag = store.find_by_field("tag_define", "tag_id", task_row["tag_id"])
    tag_defs = {row["tag_id"]: row for row in store.rows("tag_define")}
    attr_defs = _attr_defs()
    normalized: dict[str, int] = {}
    for attr_id, version in (field_versions or {}).items():
        if attr_id == "*":
            if tag is None:
                continue
            for ref in _referenced_attrs(tag, tag_defs):
                if attr_defs.get(ref, {}).get("data_type") == "enum":
                    normalized[ref] = int(version)
            continue
        normalized[str(attr_id)] = int(version)

    store.update("tag_compute", task_row["id"], {
        "requested_field_versions": normalized,
    })
    return normalized


def switch_field_versions(task_id: str, field_versions: dict[str, Any]) -> TagComputeResult:
    """登记目标版本并立即按同一份条件重算；失败时整轮不生效。"""
    request_field_versions(task_id, field_versions)
    return compute_tag(task_id)


def recompute_tag(task_id: str, new_scope: str | None = None) -> TagComputeResult:
    """重新计算标签，支持切换计算范围。"""
    task_row = store.find_by_field("tag_compute", "task_id", task_id)
    if task_row is None:
        raise ValueError(f"Task not found: {task_id}")

    if new_scope:
        store.update("tag_compute", task_row["id"], {"compute_scope": new_scope})
        # 失效相关缓存
        store.invalidate_compute_cache(f"tag_compute:{task_id}")

    return compute_tag(task_id)
