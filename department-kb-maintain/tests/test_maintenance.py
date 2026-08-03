from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

import maintain_common as common  # noqa: E402
import run_maintenance  # noqa: E402
from audit_health import audit  # noqa: E402
from build_incremental_plan import build  # noqa: E402
from build_maintenance_summary import build as build_summary  # noqa: E402
from commit_applied_state import commit  # noqa: E402
from create_incremental_job import copy_job  # noqa: E402
from finalize_incremental_plan import finalize  # noqa: E402
from initialize_baseline import initialize  # noqa: E402
from prepare_working_set import prepare  # noqa: E402
from review_gate import record_confirmation, record_local_acceptance, validate_confirmation  # noqa: E402


def source(source_id: str, *, path: str = "目录/文档", updated: str = "2026-01-01T00:00:00+08:00", content_hash: str = "hash-a") -> dict:
    return {
        "source_id": source_id,
        "department": "测试部门",
        "source_path": path,
        "file_name": Path(path).name,
        "node_id": source_id.lower(),
        "source_url": f"https://alidocs.dingtalk.com/i/nodes/{source_id.lower()}",
        "node_type": "file",
        "content_type": "ALIDOC",
        "extension": "adoc",
        "create_time": "2025-01-01T00:00:00+08:00",
        "update_time": updated,
        "creator_uid": "owner1",
        "creator_name": "创建者",
        "owner": "创建者",
        "permission_snapshot": "[]",
        "permission_hash": common.sha256_text("[]"),
        "parse_status": "全文已解析",
        "processing": "已索引",
        "status": "正式",
        "source_hash": content_hash,
        "extracted_hash": content_hash,
    }


class MaintenanceTests(unittest.TestCase):
    def make_job(self, root: Path, rows: list[dict]) -> Path:
        job = root / "job"
        (job / "00-config").mkdir(parents=True)
        (job / "01-inventory").mkdir(parents=True)
        (job / "06-reports").mkdir(parents=True)
        (job / "00-config" / "task-config.yaml").write_text(
            "task_id: TEST\nsource:\n  workspace_id: WS1\n  workspace_url: https://example.invalid/wiki\n"
            f"execution:\n  local_workbench: \"{job}\"\n  delivery_directory: \"{job}\"\n"
            "publishing:\n  enabled: false\n  target_folder_url: \"\"\n",
            encoding="utf-8",
        )
        (job / "00-config" / "maintenance-config.yaml").write_text(
            "maintenance:\n  enabled: true\n  contract_version: 1\n"
            "  separate_incremental_job_required: true\n"
            "  publish_requires_explicit_stage: true\n"
            "  require_explicit_confirmation: true\n"
            "  require_current_run_acceptance: true\n",
            encoding="utf-8",
        )
        common.write_json(job / "00-config" / "baseline-reference.json", {"baseline_job": "fixture"})
        common.write_json(job / "01-inventory" / "raw-manifest.json", rows)
        common.write_json(job / "06-reports" / "local-acceptance.json", {"passed": True})
        return job

    def test_plan_detects_new_move_and_content_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = self.make_job(Path(temporary), [source("A")])
            initialize(job)
            current_a = source("A", path="新目录/文档", updated="2026-02-01T00:00:00+08:00")
            current_b = source("B", path="目录/新增")
            common.write_json(common.state_paths(job)["latest"], {
                "schema_version": 1, "snapshot_id": "RUN1", "observed_at": common.now_iso(),
                "documents": [current_a, current_b],
            })
            plan = build(job)
            by_id = {row["source_id"]: row for row in plan["changes"]}
            self.assertIn("moved", by_id["A"]["change_flags"])
            self.assertIn("content_check", by_id["A"]["change_flags"])
            self.assertEqual(by_id["B"]["change_type"], "new")
            self.assertEqual(set(plan["actions"]["extract_source_ids"]), {"A", "B"})

    def test_periodic_hash_audit_rechecks_content_without_update_time_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = self.make_job(Path(temporary), [source("A")])
            initialize(job)
            common.write_json(common.state_paths(job)["latest"], {
                "schema_version": 1, "snapshot_id": "RUN1", "observed_at": common.now_iso(),
                "documents": [source("A")],
            })
            plan = build(job, force_hash_audit=True)
            self.assertEqual(plan["changes"][0]["change_type"], "content_check")
            self.assertEqual(plan["actions"]["extract_source_ids"], ["A"])

    def test_missing_requires_two_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = self.make_job(Path(temporary), [source("A")])
            initialize(job)
            latest = {"schema_version": 1, "snapshot_id": "RUN1", "observed_at": common.now_iso(), "documents": []}
            common.write_json(common.state_paths(job)["latest"], latest)
            first = build(job, missing_confirm_runs=2)
            self.assertEqual(first["changes"][0]["change_type"], "suspected_missing")
            repeated = build(job, missing_confirm_runs=2)
            self.assertEqual(repeated["changes"][0]["change_type"], "suspected_missing")
            self.assertEqual(repeated["changes"][0]["missing_count"], 1)
            latest["snapshot_id"] = "RUN2"
            common.write_json(common.state_paths(job)["latest"], latest)
            second = build(job, missing_confirm_runs=2)
            self.assertEqual(second["changes"][0]["change_type"], "source_orphan")

    def test_missing_creator_observation_does_not_trigger_change_or_erase_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = self.make_job(Path(temporary), [source("A")])
            initialize(job)
            current = source("A")
            current["creator_uid"] = ""
            current["creator_name"] = ""
            current["owner"] = ""
            paths = common.state_paths(job)
            common.write_json(paths["latest"], {
                "schema_version": 1, "snapshot_id": "RUN1", "observed_at": common.now_iso(),
                "documents": [current],
            })
            plan = build(job)
            self.assertEqual(plan["changes"][0]["change_type"], "unchanged")

            # Exercise the preservation rule through a path-only change, which
            # creates an actionable working set without requiring extraction.
            current["source_path"] = "新目录/文档"
            common.write_json(paths["latest"], {
                "schema_version": 1, "snapshot_id": "RUN2", "observed_at": common.now_iso(),
                "documents": [current],
            })
            build(job)
            rows = prepare(job)
            self.assertEqual(rows[0]["creator_uid"], "owner1")
            self.assertEqual(rows[0]["creator_name"], "创建者")
            self.assertEqual(rows[0]["owner"], "创建者")

    def test_finalize_uses_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = self.make_job(Path(temporary), [source("A")])
            initialize(job)
            paths = common.state_paths(job)
            common.write_json(paths["plan"], {
                "schema_version": 1, "run_id": "RUN", "snapshot_id": "RUN",
                "changes": [{"source_id": "A", "change_type": "content_check", "change_flags": ["content_check"]}],
                "counts": {"content_check": 1}, "actions": {},
            })
            common.write_json(job / "01-inventory" / "raw-admission" / "excluded-manifest.json", [])
            plan = finalize(job)
            self.assertEqual(plan["changes"][0]["change_type"], "metadata_only")

    def test_finalize_distinguishes_file_change_from_extracted_text_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = self.make_job(Path(temporary), [source("A", content_hash="raw-old")])
            initialize(job)
            row = source("A", content_hash="raw-new")
            row["extracted_hash"] = "raw-old"
            common.write_json(job / "01-inventory" / "raw-manifest.json", [row])
            paths = common.state_paths(job)
            common.write_json(paths["plan"], {
                "schema_version": 1, "run_id": "RUN", "snapshot_id": "RUN",
                "changes": [{"source_id": "A", "change_type": "content_check", "change_flags": ["content_check"]}],
                "counts": {"content_check": 1}, "actions": {},
            })
            common.write_json(job / "01-inventory" / "raw-admission" / "excluded-manifest.json", [])
            plan = finalize(job)
            self.assertEqual(plan["changes"][0]["change_type"], "file_changed_text_unchanged")

    def test_sensitive_attachment_query_is_removed_from_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            row = source("A")
            row["source_url"] = "https://example.invalid/a.xlsx?Expires=1&OSSAccessKeyId=x&Signature=y"
            job = self.make_job(Path(temporary), [row])
            initialize(job)
            common.write_json(common.state_paths(job)["latest"], {
                "schema_version": 1, "snapshot_id": "RUN1", "observed_at": common.now_iso(),
                "documents": [],
            })
            plan = build(job)
            self.assertEqual(plan["changes"][0]["source_url"], "https://example.invalid/a.xlsx")

    def test_embedded_attachment_is_not_marked_missing_by_tree_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            attachment = source("TB-ATT-123456789ABC")
            attachment["virtual_kind"] = "embedded_attachment"
            attachment["parent_source_id"] = "TB-PARENT"
            job = self.make_job(Path(temporary), [attachment])
            initialize(job)
            common.write_json(common.state_paths(job)["latest"], {
                "schema_version": 1, "snapshot_id": "RUN1", "observed_at": common.now_iso(),
                "documents": [],
            })
            plan = build(job)
            self.assertEqual(plan["changes"], [])
            self.assertEqual(plan["counts"], {})

    def test_health_reports_stale_isolated_and_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = self.make_job(Path(temporary), [source("A", updated="2020-01-01T00:00:00+08:00")])
            paths = common.state_paths(job)
            common.write_json(paths["plan"], {
                "run_id": "RUN", "changes": [{
                    "source_id": "X", "title": "失联文档", "source_url": "https://example.invalid",
                    "change_type": "source_orphan", "missing_count": 2,
                }],
            })
            profile_dir = job / "02-extraction-cache" / "semantic" / "source-profiles"
            common.write_json(profile_dir / "A.json", {
                "source_id": "A", "content_profile": {"version_signals": ["本制度已废止"]},
            })
            preview = job / "本地审核结果（仅供审核）"
            for root in (preview / "01-原文镜像（按原目录）" / "目录", preview / "02-蒸馏结果（按文档类型）" / "制度"):
                root.mkdir(parents=True)
                (root / "目录索引.md").write_text("索引", encoding="utf-8")
                (root / "文档（稳定ID：A）.md").write_text("正文", encoding="utf-8")
            ledger = job / "05-ledgers" / "relation-verification.csv"
            common.write_csv(ledger, [{
                "source_id": "A", "source_title": "文档", "source_url": "https://example.invalid/a",
                "target_id": "B", "review_level": "L3", "risk_flags": json.dumps(["冲突"], ensure_ascii=False),
                "verification_reason": "双方口径冲突", "verified_relation_type": "冲突",
            }])
            common.write_csv(job / "05-ledgers" / "relation-publication-ready.csv", [])
            report = audit(job, stale_days=180)
            kinds = {row["finding_type"] for row in report["findings"]}
            self.assertIn("来源孤立", kinds)
            self.assertIn("长期未复查", kinds)
            self.assertIn("疑似失效或被替代", kinds)
            self.assertIn("关系孤立", kinds)
            self.assertIn("新冲突", kinds)
            user_report = (job / "06-reports" / "知识库健康检查.md").read_text(encoding="utf-8")
            self.assertNotIn("[P0]", user_report)
            self.assertNotIn("[P1]", user_report)
            self.assertNotIn("[P2]", user_report)
            self.assertIn("需要立即处理", user_report)

    def test_user_facing_summary_uses_plain_language(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = self.make_job(Path(temporary), [source("A")])
            paths = common.state_paths(job)
            common.write_json(paths["plan"], {
                "run_id": "RUN1", "counts": {"new": 1},
                "changes": [{"source_id": "A", "title": "新增制度", "change_type": "new", "source_url": "https://example.invalid/a"}],
            })
            common.write_json(paths["health"], {
                "counts": {"by_severity": {"P0": 1, "P1": 1, "P2": 1}, "new_conflicts": 1},
                "findings": [{
                    "severity": "P0", "finding_type": "新冲突", "source_id": "A", "title": "新增制度",
                    "source_url": "https://example.invalid/a", "related_source_id": "B", "related_title": "旧制度",
                    "related_url": "https://example.invalid/b", "detail": "两份文档说法不一致", "recommended_action": "确认哪份现在有效",
                }],
            })
            build_summary(job, mode="local", processed=True)
            text = (job / "00-增量结果与下一步.md").read_text(encoding="utf-8")
            review = (job / "01-本次增量审核入口.md").read_text(encoding="utf-8")
            for term in ("P0", "P1", "P2", "成功基线", "--stage", "Raw"):
                self.assertNotIn(term, text)
                self.assertNotIn(term, review)
            self.assertIn("需要立即处理", text)
            self.assertIn("下一步", text)

    def test_local_acceptance_and_confirmation_are_bound_to_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = self.make_job(Path(temporary), [source("A")])
            initialize(job)
            paths = common.state_paths(job)
            common.write_json(paths["latest"], {
                "schema_version": 1, "snapshot_id": "RUN1", "observed_at": common.now_iso(),
                "documents": [source("A")],
            })
            common.write_json(paths["plan"], {
                "schema_version": 1, "run_id": "RUN1", "snapshot_id": "RUN1",
                "phase": "post_extraction", "generated_at": common.now_iso(),
                "status": "changes_detected", "changes": [], "counts": {},
                "actions": {
                    "extract_source_ids": [], "semantic_source_ids": [], "affected_source_ids": [],
                    "processing_source_ids": ["A"], "requires_relation_rebuild": True,
                    "requires_render": True, "requires_health_audit": True,
                },
            })
            record_local_acceptance(job, ["A"])
            with self.assertRaises(RuntimeError):
                commit(job)
            with self.assertRaises(RuntimeError):
                record_confirmation(job, "ok", "审核人")
            record_confirmation(job, "确认本次增量蒸馏没有问题", "审核人")
            self.assertEqual(validate_confirmation(job)["confirmed_by"], "审核人")
            with self.assertRaises(RuntimeError):
                commit(job, require_readback=True)
            committed = commit(job)
            self.assertEqual(committed["last_successful_run"]["confirmed_by"], "审核人")
            plan = common.load_json(paths["plan"], {})
            plan["counts"] = {"new": 1}
            common.write_json(paths["plan"], plan)
            with self.assertRaises(RuntimeError):
                validate_confirmation(job)

    def test_all_stage_never_commits_or_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = self.make_job(Path(temporary), [source("A")])
            initialize(job)
            arguments = ["run_maintenance.py", "--job", str(job), "--stage", "all"]
            with (
                patch.object(sys, "argv", arguments),
                patch.object(run_maintenance, "ensure_config"),
                patch.object(run_maintenance.common, "find_base_skill", return_value=Path("/fake/base")),
                patch.object(run_maintenance.common, "task_lock", return_value=common.task_lock(job)),
                patch.object(run_maintenance, "scan"),
                patch.object(run_maintenance, "build_plan", return_value={"changes": []}),
                patch.object(run_maintenance, "process_changes", return_value=True),
                patch.object(run_maintenance, "audit"),
                patch.object(run_maintenance, "build_summary") as summary,
                patch.object(run_maintenance, "commit") as commit_mock,
                patch.object(run_maintenance, "base_pipeline") as base_pipeline_mock,
            ):
                run_maintenance.main()
            commit_mock.assert_not_called()
            base_pipeline_mock.assert_not_called()
            summary.assert_called_once_with(job.resolve(), mode="local", processed=True, published=False, committed=False)

    def test_create_incremental_job_copies_accepted_baseline_into_separate_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self.make_job(root, [source("A")])
            preview = baseline / "本地审核结果（仅供审核）"
            preview.mkdir(parents=True)
            (preview / "审核入口.md").write_text("审核", encoding="utf-8")
            (baseline / "00-config" / "钉钉发布授权.json").write_text("{}", encoding="utf-8")
            target = root / "incremental-job"
            reference = copy_job(baseline, target, task_id="TEST-INC")
            self.assertEqual(reference["incremental_task_id"], "TEST-INC")
            self.assertTrue((target / "00-config" / "baseline-reference.json").exists())
            self.assertTrue((target / "00-config" / "maintenance-config.yaml").exists())
            self.assertFalse((target / "00-config" / "钉钉发布授权.json").exists())
            self.assertIn("TEST-INC", (target / "00-config" / "task-config.yaml").read_text(encoding="utf-8"))
            self.assertFalse(common.state_paths(target)["applied"].exists())


if __name__ == "__main__":
    unittest.main()
