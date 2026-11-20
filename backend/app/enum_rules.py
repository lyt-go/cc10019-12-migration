"""枚举字段版本升级与条件兼容解析的纯规则函数。

迁移明细生成和标签计算运行时都调用这里，保证“原值解释”只有一套口径：
- 枚举字段第 2 版用稳定代码表达含义；
- 旧显示名通过 (属性, 采集来源, 显示名) -> 代码 的兼容映射解析；
- 缺少采集来源或同一显示名对应多个代码时，拒绝迁移/拒绝切换。
"""
from __future__ import annotations

import re
from typing import Any

LEGACY_FIELD_VERSION = 1
CODE_FIELD_VERSION = 2

# 只解析以 ATTR- 开头的字段，TAG-xxx 的布尔引用不参与枚举映射。
_IN_CLAUSE = re.compile(r"(ATTR-[A-Za-z0-9_-]+)\s+((?:NOT\s+)?IN)\s*\[([^\]]*)\]", re.IGNORECASE)
_EQ_CLAUSE = re.compile(r"(ATTR-[A-Za-z0-9_-]+)\s*(?:==|!=|=)\s*(['\"])(.*?)\2")
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def normalize_source(source: Any) -> str:
    """采集来源统一去掉首尾空白；空串表示来源缺失。"""
    return str(source or "").strip()


def compat_entries(attr: dict[str, Any]) -> list[dict[str, str]]:
    """读取字段定义里的兼容映射，按原顺序返回。"""
    return [
        {
            "display": str(item.get("display", "")),
            "source": normalize_source(item.get("source")),
            "code": str(item.get("code", "")),
        }
        for item in (attr.get("compat_map") or [])
    ]


def migration_outcome(attr: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
    """判断一条存量样本能否迁移到第 2 版代码，并给出可复核的原因。"""
    display = str(sample.get("raw_value", ""))
    source = normalize_source(sample.get("source"))
    entries = [item for item in compat_entries(attr) if item["display"] == display]
    candidates = [{"source": item["source"], "code": item["code"]} for item in entries]

    # 缺来源的记录一律不迁移：显示名相同不代表原值代码相同。
    if not source:
        return {
            "status": "blocked_missing_source",
            "code": None,
            "reason": "缺采集来源，无法证明显示名对应的原值代码，未迁移",
            "candidates": candidates,
        }

    exact = [item for item in entries if item["source"] == source]
    codes = sorted({item["code"] for item in exact})
    if len(codes) == 1:
        return {
            "status": "migrated",
            "code": codes[0],
            "reason": "按采集来源命中唯一兼容映射",
            "candidates": candidates,
        }
    if len(codes) > 1:
        return {
            "status": "ambiguous",
            "code": None,
            "reason": "同一采集来源命中多个代码，未迁移",
            "candidates": candidates,
        }
    return {
        "status": "unmapped",
        "code": None,
        "reason": f"采集来源 {source} 缺少该显示名的兼容映射，未迁移",
        "candidates": candidates,
    }


def parse_condition_literals(logic: str) -> dict[str, list[str]]:
    """从条件表达式中提取每个属性引用的显示名/字面量。

    支持 ``ATTR-X IN ['a', 'b']`` 与 ``ATTR-X = 'a'`` / ``!=`` / ``==``。
    数值和布尔字面量也会被提取，但只有枚举字段会进入兼容映射解析。
    """
    grouped: dict[str, list[str]] = {}

    def add(attr_id: str, value: str) -> None:
        bucket = grouped.setdefault(attr_id, [])
        if value not in bucket:
            bucket.append(value)

    for match in _IN_CLAUSE.finditer(logic or ""):
        attr_id, body = match.group(1), match.group(3)
        for quoted in _QUOTED.finditer(body):
            value = quoted.group(1) if quoted.group(1) is not None else quoted.group(2)
            add(attr_id, value)

    for match in _EQ_CLAUSE.finditer(logic or ""):
        add(match.group(1), match.group(3))

    return grouped


def resolve_condition_literal(
    attr: dict[str, Any],
    display: str,
    declared_source: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """把旧条件里的显示名解析成第 2 版代码。

    返回 ``(resolution, issue)``：
    - 有唯一可证的原值解释时 resolution 带 code 与证据；
    - 否则 issue 说明失败原因，调用方必须中止整轮计算。
    """
    source = normalize_source(declared_source)
    entries = [item for item in compat_entries(attr) if item["display"] == display]
    candidates = [{"source": item["source"], "code": item["code"]} for item in entries]

    if source:
        exact = [item for item in entries if item["source"] == source]
        codes = sorted({item["code"] for item in exact})
        if len(codes) == 1:
            return (
                {
                    "code": codes[0],
                    "source": source,
                    "source_origin": "declared",
                    "candidates": candidates,
                },
                None,
            )
        if len(codes) > 1:
            return None, {
                "code": "ambiguous_original_value",
                "reason": f"来源 {source} 下显示名 {display} 命中多个代码",
                "candidates": candidates,
            }
        return None, {
            "code": "missing_source_mapping",
            "reason": f"来源 {source} 下缺少显示名 {display} 的兼容映射",
            "candidates": candidates,
        }

    # 未声明来源时，只有所有来源都指向同一个代码才算“唯一可证”。
    code_sources: dict[str, list[str]] = {}
    for item in entries:
        code_sources.setdefault(item["code"], []).append(item["source"])
    if len(code_sources) == 1:
        code = next(iter(code_sources))
        return (
            {
                "code": code,
                "source": "",
                "source_origin": "unique_across_sources",
                "candidates": candidates,
            },
            None,
        )
    if len(code_sources) > 1:
        return None, {
            "code": "ambiguous_original_value",
            "reason": (
                f"显示名 {display} 在不同采集来源对应不同代码，"
                "缺少唯一可证的原值解释"
            ),
            "candidates": candidates,
        }
    return None, {
        "code": "unmapped_literal",
        "reason": f"显示名 {display} 不在字段 {attr.get('attr_id', '')} 的兼容映射中",
        "candidates": candidates,
    }


class ConditionSwitchError(Exception):
    """条件无法整体切到目标字段版本：本轮计算必须中止，不能混用新旧口径。"""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        super().__init__("; ".join(str(item.get("reason", "")) for item in issues))


def resolve_condition(
    logic: str,
    attr_defs: dict[str, dict[str, Any]],
    declared_sources: dict[str, str],
    target_versions: dict[str, int],
) -> dict[str, Any]:
    """把整条条件一次性解析到目标字段版本。

    先解析全部字面量，任一失败就抛 ``ConditionSwitchError``，不产出半新半旧的条件。
    成功时返回新的条件表达式、逐字面量证据和本轮引用的字段版本。
    """
    literals = parse_condition_literals(logic)
    resolutions: dict[tuple[str, str], dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    versions_used: dict[str, int] = {}

    for attr_id, values in literals.items():
        attr = attr_defs.get(attr_id)
        if attr is None:
            issues.append({
                "attr_id": attr_id,
                "code": "unknown_attribute",
                "reason": f"条件引用了未定义的属性 {attr_id}",
            })
            continue
        version = int(target_versions.get(attr_id, attr.get("field_version", LEGACY_FIELD_VERSION)))
        versions_used[attr_id] = version
        for display in values:
            if attr.get("data_type") != "enum" or version < CODE_FIELD_VERSION:
                resolutions[(attr_id, display)] = {
                    "attr_id": attr_id,
                    "from": display,
                    "to": display,
                    "field_version": version,
                    "converted": False,
                    "evidence": "非枚举字段或仍按第 1 版口径，保持原字面量",
                }
                continue
            resolution, issue = resolve_condition_literal(
                attr, display, declared_sources.get(attr_id, "")
            )
            if issue is not None:
                issues.append({**issue, "attr_id": attr_id, "from": display})
                continue
            resolutions[(attr_id, display)] = {
                "attr_id": attr_id,
                "from": display,
                "to": resolution["code"],
                "field_version": version,
                "converted": True,
                "source_origin": resolution["source_origin"],
                "evidence": resolution,
            }

    if issues:
        raise ConditionSwitchError(issues)

    def replace_in(match: re.Match[str]) -> str:
        attr_id = match.group(1)

        def replace_literal(literal_match: re.Match[str]) -> str:
            raw = literal_match.group(1) if literal_match.group(1) is not None else literal_match.group(2)
            resolution = resolutions.get((attr_id, raw))
            if resolution is None or not resolution["converted"]:
                return literal_match.group(0)
            quote = literal_match.group(0)[0]
            return f"{quote}{resolution['to']}{quote}"

        body = _QUOTED.sub(replace_literal, match.group(3))
        return f"{attr_id} {match.group(2)} [{body}]"

    def replace_eq(match: re.Match[str]) -> str:
        attr_id, quote, raw = match.group(1), match.group(2), match.group(3)
        resolution = resolutions.get((attr_id, raw))
        if resolution is None or not resolution["converted"]:
            return match.group(0)
        return match.group(0).replace(f"{quote}{raw}{quote}", f"{quote}{resolution['to']}{quote}", 1)

    new_logic = _IN_CLAUSE.sub(replace_in, logic or "")
    new_logic = _EQ_CLAUSE.sub(replace_eq, new_logic)

    return {
        "logic": new_logic,
        "resolutions": list(resolutions.values()),
        "field_versions": versions_used,
    }
