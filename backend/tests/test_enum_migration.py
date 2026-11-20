"""枚举字段第 2 版迁移与标签计算口径测试。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.enum_rules import (
    ConditionSwitchError,
    migration_outcome,
    parse_condition_literals,
    resolve_condition,
)
from app.services.attr_dict import (
    apply_migration,
    build_migration_detail,
    get_attr,
    get_migration_detail,
)
from app.services.tag_compute import (
    ComputeSwitchError,
    compute_tag,
    list_run_snapshots,
    replay_task,
    switch_field_versions,
)
from app.store import store


def _reset() -> None:
    """恢复属性定义与迁移状态，避免用例之间互相污染。"""
    from app.seed import SEED_ROWS

    store._tables["attr_dict"] = [dict(row) for row in SEED_ROWS["attr_dict"]]
    store._tables["attr_enum_sample"] = [dict(row) for row in SEED_ROWS["attr_enum_sample"]]
    store._tables["tag_compute"] = [dict(row) for row in SEED_ROWS["tag_compute"]]
    store._artifacts.clear()
    store._snapshots.clear()
    store._compute_cache.clear()


class TestEnumMigration(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def test_plain_value_maps_per_source(self) -> None:
        """“普通”在不同采集来源对应不同代码，迁移明细逐条可查。"""
        detail = build_migration_detail("ATTR-005")
        by_id = {row["sample_id"]: row for row in detail["details"]}

        self.assertEqual(by_id["AES-001"]["code"], "NORMAL")
        self.assertEqual(by_id["AES-001"]["source"], "event")
        self.assertEqual(by_id["AES-002"]["code"], "BASIC")
        self.assertEqual(by_id["AES-002"]["source"], "import")
        self.assertTrue(by_id["AES-001"]["migrated"])
        self.assertTrue(by_id["AES-002"]["migrated"])

    def test_missing_source_is_not_migrated(self) -> None:
        """缺来源的记录不会被误迁，且不会因显式排除被洗成可迁移。"""
        detail = build_migration_detail("ATTR-005")
        missing = {row["sample_id"]: row for row in detail["details"]}["AES-003"]
        self.assertEqual(missing["status"], "blocked_missing_source")
        self.assertFalse(missing["migrated"])
        self.assertIsNone(missing["code"])

        applied = apply_migration("ATTR-005")
        self.assertIn("AES-003", applied["blocked_missing_source"])
        self.assertIn("AES-003", applied["unmigrated_sample_ids"])
        self.assertNotIn("AES-003", applied["migrated_sample_ids"])

    def test_apply_upgrades_field_to_version_2_and_keeps_legacy(self) -> None:
        """迁移后字段定义切到第 2 版，同时保留第 1 版显示名与兼容映射。"""
        applied = apply_migration("ATTR-005")
        attr = get_attr("ATTR-005")
        self.assertEqual(attr["field_version"], 2)
        self.assertEqual(attr["enum_values"], ["NORMAL", "BASIC", "SILVER", "GOLD", "DIAMOND"])
        self.assertIn("普通", attr["legacy_enum_values"])
        self.assertTrue(attr["compat_map"])
        self.assertEqual(applied["result"], "applied")

        persisted = get_migration_detail("ATTR-005")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["target_field_version"], 2)

    def test_unmapped_sample_blocks_apply_until_excluded(self) -> None:
        """存在无法映射且未排除的样本时，迁移整次中止。"""
        for sample in store.rows("attr_enum_sample"):
            if sample["sample_id"] == "AES-006":
                sample["excluded"] = False

        detail = build_migration_detail("ATTR-005")
        self.assertFalse(detail["can_apply"])

        with self.assertRaises(ValueError):
            apply_migration("ATTR-005")
        self.assertEqual(get_attr("ATTR-005")["field_version"], 1)

        detail = build_migration_detail("ATTR-005", excluded_sample_ids=["AES-006"])
        self.assertTrue(detail["can_apply"])


class TestConditionResolution(unittest.TestCase):
    def setUp(self) -> None:
        _reset()
        apply_migration("ATTR-005")
        self.attr = get_attr("ATTR-005")

    def test_literals_extracted_from_in_and_eq(self) -> None:
        grouped = parse_condition_literals("ATTR-005 IN ['金卡', '钻石'] AND ATTR-004 = 'ios'")
        self.assertEqual(grouped["ATTR-005"], ["金卡", "钻石"])
        self.assertEqual(grouped["ATTR-004"], ["ios"])

    def test_declared_source_gives_unique_code(self) -> None:
        resolved = resolve_condition(
            "ATTR-005 IN ['金卡', '钻石']",
            {"ATTR-005": self.attr},
            {"ATTR-005": "event"},
            {"ATTR-005": 2},
        )
        self.assertEqual(resolved["logic"], "ATTR-005 IN ['GOLD', 'DIAMOND']")
        self.assertEqual(resolved["field_versions"], {"ATTR-005": 2})

    def test_plain_name_without_source_is_ambiguous(self) -> None:
        """没有来源声明时，“普通”对应多个代码，必须拒绝切换。"""
        with self.assertRaises(ConditionSwitchError) as ctx:
            resolve_condition(
                "ATTR-005 IN ['普通', '金卡']",
                {"ATTR-005": self.attr},
                {},
                {"ATTR-005": 2},
            )
        codes = [issue["code"] for issue in ctx.exception.issues]
        self.assertIn("ambiguous_original_value", codes)

    def test_unique_across_sources_is_allowed(self) -> None:
        """显示名在所有来源下都指向同一代码时，缺来源声明也有唯一解释。"""
        resolved = resolve_condition(
            "ATTR-005 = '银卡'",
            {"ATTR-005": self.attr},
            {},
            {"ATTR-005": 2},
        )
        self.assertIn("SILVER", resolved["logic"])
        self.assertEqual(resolved["resolutions"][0]["source_origin"], "unique_across_sources")

    def test_switch_is_all_or_nothing(self) -> None:
        """一个字面量失败就整轮失败，不产出半新半旧的条件。"""
        with self.assertRaises(ConditionSwitchError) as ctx:
            resolve_condition(
                "ATTR-005 IN ['银卡', '普通']",
                {"ATTR-005": self.attr},
                {"ATTR-005": "h5"},
                {"ATTR-005": 2},
            )
        # “银卡”本来能解析成 SILVER，但整轮仍因“普通”无映射而失败。
        failed = {issue["from"] for issue in ctx.exception.issues}
        self.assertEqual(failed, {"普通", "银卡"})


class TestTagComputeVersions(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def test_legacy_task_replays_old_condition(self) -> None:
        """旧任务按当时的条件口径回查，不随字段定义漂移。"""
        legacy = compute_tag("TC-005")
        self.assertEqual(legacy.field_versions, {"ATTR-005": 1})
        self.assertIn("普通", legacy.condition_used)
        self.assertGreater(legacy.matched_count, 0)

        apply_migration("ATTR-005")
        replayed = replay_task("TC-005")
        self.assertEqual(replayed["mode"], "replay")
        self.assertEqual(replayed["field_versions"], {"ATTR-005": 1})
        self.assertIn("普通", replayed["condition_used"])
        self.assertEqual(replayed["matched_count"], legacy.matched_count)

    def test_switch_tag_a_uses_codes(self) -> None:
        """标签甲（有来源声明）能按新版代码计算，实际启用的版本被记录。"""
        legacy = compute_tag("TC-003")
        self.assertEqual(legacy.field_versions["ATTR-005"], 1)

        apply_migration("ATTR-005")
        result = switch_field_versions("TC-003", {"ATTR-005": 2})
        self.assertEqual(result.mode, "switch")
        self.assertEqual(result.field_versions["ATTR-005"], 2)
        self.assertEqual(result.field_versions["ATTR-002"], 1)
        self.assertIn("GOLD", result.condition_used)
        self.assertNotIn("金卡", result.condition_used)
        # 缺来源的用户无法解释成代码，只能计为未映射，不会被算成某个枚举值。
        self.assertGreater(result.unmapped_user_count, 0)

        artifact = store.get_artifact("tag_compute", "TC-003")
        self.assertEqual(artifact["field_versions"]["ATTR-005"], 2)
        task = store.find_by_field("tag_compute", "task_id", "TC-003")
        self.assertEqual(task["field_versions"]["ATTR-005"], 2)

    def test_switch_tag_b_fails_without_mixing(self) -> None:
        """标签乙只存显示名，缺唯一原值解释，切换整轮失败且不混用新旧口径。"""
        apply_migration("ATTR-005")
        legacy = compute_tag("TC-005")
        before = store.get_artifact("tag_compute", "TC-005")

        with self.assertRaises(ComputeSwitchError) as ctx:
            switch_field_versions("TC-005", {"ATTR-005": 2})
        self.assertIn("ambiguous_original_value", [i["code"] for i in ctx.exception.issues])

        after = store.get_artifact("tag_compute", "TC-005")
        self.assertEqual(after["matched_count"], legacy.matched_count)
        self.assertEqual(after["field_versions"], {"ATTR-005": 1})
        self.assertEqual(after["condition_used"], before["condition_used"])

        task = store.find_by_field("tag_compute", "task_id", "TC-005")
        self.assertEqual(task["field_versions"], {"ATTR-005": 1})
        self.assertEqual(task["requested_field_versions"], {"ATTR-005": 2})
        self.assertEqual(task["status"], "失败")

        # 旧命中仍然可以按 v1 回查。
        self.assertEqual(replay_task("TC-005")["matched_count"], legacy.matched_count)

    def test_switch_before_migration_is_rejected(self) -> None:
        """字段还没迁移时不能直接提交第 2 版计算。"""
        with self.assertRaises(ComputeSwitchError) as ctx:
            switch_field_versions("TC-003", {"ATTR-005": 2})
        self.assertIn("field_version_not_ready", [i["code"] for i in ctx.exception.issues])
        task = store.find_by_field("tag_compute", "task_id", "TC-003")
        self.assertEqual(task["field_versions"], {"ATTR-005": 1})

    def test_interrupted_switch_then_rerun_is_consistent(self) -> None:
        """切换中断后按相同条件重算，生效版本与条件口径一致。"""
        apply_migration("ATTR-005")
        with self.assertRaises(ComputeSwitchError) as ctx:
            compute_tag("TC-003", interrupt=True)
        self.assertEqual(ctx.exception.issues[0]["code"], "switch_interrupted")

        # 中断那一轮没有结果产物
        self.assertIsNone(store.get_artifact("tag_compute", "TC-003"))
        task = store.find_by_field("tag_compute", "task_id", "TC-003")
        self.assertEqual(task["field_versions"], {"ATTR-005": 1})

        rerun = compute_tag("TC-003")
        self.assertEqual(rerun.field_versions["ATTR-005"], 1)
        self.assertIn("金卡", rerun.condition_used)

        switched = switch_field_versions("TC-003", {"ATTR-005": 2})
        again = compute_tag("TC-003")
        self.assertEqual(switched.condition_used, again.condition_used)
        self.assertEqual(switched.field_versions, again.field_versions)

        runs = list_run_snapshots("TC-003")
        self.assertEqual(
            [run["field_versions"]["ATTR-005"] for run in runs],
            [1, 1, 2, 2],
        )
        self.assertEqual(runs[0]["exec_state"], "failed")

    def test_every_run_records_enabled_versions(self) -> None:
        """每次计算都留下运行快照，记录实际启用的字段版本。"""
        apply_migration("ATTR-005")
        compute_tag("TC-003")
        switch_field_versions("TC-003", {"ATTR-005": 2})
        runs = list_run_snapshots("TC-003")
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertIn("field_versions", run)
            self.assertTrue(run["field_versions"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
