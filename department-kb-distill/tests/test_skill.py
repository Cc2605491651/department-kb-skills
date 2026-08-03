from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from apply_raw_admission import classify  # noqa: E402
from build_delivery_summary import aggregate_remote  # noqa: E402
from config_utils import load_config, publication_target, validate_config_schema  # noqa: E402
from enrich_creators import contact_name_map, creator_from_search, creator_search_queries  # noqa: E402
from kb_common import sanitize_transient_url, sanitize_transient_urls  # noqa: E402
from render_review_package import (  # noqa: E402
    AI_HEADER_FIELDS,
    DISTILLATION_PROFILE_FIELDS,
    STANDARD_METADATA_FIELDS,
    build_standard_metadata,
    unified_header_markdown,
    unified_header_payload,
)
from readback_wiki import verify_content  # noqa: E402
from publish_wiki import should_update_page  # noqa: E402
from wiki_publish_common import (  # noqa: E402
    PublicationPage,
    discover_pages,
    render_page,
    sha256_bytes,
    split_markdown_at_block_boundaries,
    verify_publication_config,
)


class ConfigTests(unittest.TestCase):
    def test_nested_template_is_read_without_pyyaml(self) -> None:
        config = load_config(SKILL_ROOT / "assets" / "task-config.yaml")
        self.assertEqual(config.get("content.required_metadata_fields"), 17)
        self.assertEqual(config.get("relation_processing.l2_sample_rate"), 0.2)
        self.assertEqual(config.get_list("publishing.allowed_source_views"), [
            "01-原文镜像（按原目录）",
            "02-蒸馏结果（按文档类型）",
        ])

    def test_bootstrap_turns_publish_target_into_task_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary) / "job"
            target = "https://alidocs.dingtalk.com/i/nodes/TEST_TARGET"
            result = subprocess.run([
                sys.executable,
                str(SCRIPTS / "bootstrap_job.py"),
                "--job", str(job),
                "--task-id", "TEST-001",
                "--department", "测试部",
                "--workspace-id", "SPACE1",
                "--workspace-url", "https://alidocs.dingtalk.com/i/spaces/SPACE1/overview",
                "--workspace-name", "测试知识库",
                "--id-prefix", "TEST",
                "--executed-by", "测试执行人",
                "--publish-target", target,
                "--publish-root-name", "测试发布根目录",
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_config(job)
            self.assertEqual(publication_target(config), target)
            self.assertEqual(config.get("execution_identity.owner_source"), "dingtalk_creator")
            (job / "06-reports").mkdir(parents=True, exist_ok=True)
            (job / "06-reports" / "local-acceptance.json").write_text('{"passed": true}\n', encoding="utf-8")
            authority = verify_publication_config(job, target)
            self.assertEqual(authority["target_url"], target)
            self.assertEqual(validate_config_schema(config, SKILL_ROOT / "schemas" / "task-config.schema.json"), [])
            with self.assertRaises(RuntimeError):
                verify_publication_config(job, "https://alidocs.dingtalk.com/i/nodes/OTHER")

    def test_schema_rejects_dangerous_publication_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "task-config.yaml"
            text = (SKILL_ROOT / "assets" / "task-config.yaml").read_text(encoding="utf-8")
            path.write_text(text.replace("allow_delete: false", "allow_delete: true"), encoding="utf-8")
            errors = validate_config_schema(load_config(path), SKILL_ROOT / "schemas" / "task-config.schema.json")
            self.assertTrue(any("publishing.allow_delete" in error for error in errors))


class RawAdmissionTests(unittest.TestCase):
    def test_allowed_formats(self) -> None:
        for extension in ("adoc", "docx", "pdf", "pptx", "xlsx", "axls", "csv", "md", "txt"):
            self.assertTrue(classify({"extension": extension})[0], extension)

    def test_rejected_formats(self) -> None:
        for extension in ("jpg", "png", "mp3", "mp4", "zip", "rar", "exe"):
            self.assertFalse(classify({"extension": extension})[0], extension)


class PublicationTests(unittest.TestCase):
    def test_delivery_summary_marks_changed_local_page_as_stale(self) -> None:
        self.assertEqual(
            aggregate_remote([{"update_status": "stale"}], "update_status", "成功", "未执行"),
            "待更新（本地页面已变化）",
        )

    def test_creator_search_and_contact_mapping_feed_owner(self) -> None:
        search_payload = {
            "documents": [
                {"nodeId": "OTHER", "creatorUid": "wrong"},
                {"nodeId": "NODE-1", "creatorUid": "example-user"},
            ]
        }
        uid, name = creator_from_search(search_payload, "NODE-1")
        self.assertEqual((uid, name), ("example-user", ""))
        contacts = contact_name_map({
            "result": [{"orgEmployeeModel": {"orgUserId": "example-user", "orgUserName": "示例用户"}}]
        })
        self.assertEqual(contacts[uid], "示例用户")
        queries = creator_search_queries('示例项目 Q3/Q4 KPI Catch-up 纪要 · 复盘"关键指标"')
        self.assertIn("示例项目 Q3 Q4 KPI Catch up 纪要 复盘 关键指标", queries)
        self.assertTrue(any(len(value) <= 32 for value in queries))

    def test_temporary_signed_urls_are_redacted(self) -> None:
        signed = "https://oss.example.com/file.xlsx?Expires=1&OSSAccessKeyId=public&Signature=secret"
        self.assertEqual(sanitize_transient_url(signed), "https://oss.example.com/file.xlsx")
        self.assertEqual(
            sanitize_transient_url(signed, "PARENT_NODE"),
            "https://alidocs.dingtalk.com/i/nodes/PARENT_NODE",
        )
        self.assertNotIn("Signature=", sanitize_transient_urls(f"附件：{signed}。"))

    def test_metadata_update_time_is_deterministic(self) -> None:
        profile = {
            "source_id": "TEST-1",
            "generated_at": "2026-07-01T10:00:00+08:00",
            "update_time": "2026-06-30T09:00:00+08:00",
            "content_profile": {},
        }
        ready = [{
            "source_id": "TEST-1",
            "target_id": "TEST-2",
            "verified_at": "2026-07-02T11:00:00+08:00",
        }]
        first = build_standard_metadata(profile, ready)
        second = build_standard_metadata(profile, ready)
        self.assertEqual(first["property_updated_at"], "2026-07-02 11:00:00")
        self.assertEqual(first, second)

    def test_explicit_hash_bound_acceptance_promotes_status(self) -> None:
        profile = {
            "source_id": "TEST-1",
            "source_hash": "a" * 64,
            "file_name": "示例",
            "source_url": "https://alidocs.dingtalk.com/i/nodes/TEST-1",
            "creator_name": "创建者",
            "content_profile": {"summary": "摘要"},
        }
        acceptance = {
            "decision": "confirmed",
            "confirmed_at": "2026-07-31T10:00:00+08:00",
            "documents": [{"source_id": "TEST-1", "content_hash": "a" * 64}],
        }
        self.assertEqual(build_standard_metadata(profile, [], acceptance)["status"], "正式")
        profile["source_hash"] = "b" * 64
        self.assertEqual(build_standard_metadata(profile, [], acceptance)["status"], "候选")

    def test_confirmation_script_records_dialogue_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary) / "job"
            (job / "00-config").mkdir(parents=True)
            (job / "00-config" / "task-config.yaml").write_text(
                (SKILL_ROOT / "assets" / "task-config.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (job / "01-inventory").mkdir(parents=True)
            (job / "01-inventory" / "raw-manifest.json").write_text(json.dumps([{
                "source_id": "TEST-1", "parse_status": "全文已解析",
                "creator_name": "创建者", "owner": "创建者",
            }], ensure_ascii=False), encoding="utf-8")
            profile_dir = job / "02-extraction-cache" / "semantic" / "source-profiles"
            profile_dir.mkdir(parents=True)
            (profile_dir / "TEST-1.json").write_text(json.dumps({
                "source_id": "TEST-1", "source_hash": "a" * 64,
                "creator_name": "创建者", "owner": "创建者",
            }, ensure_ascii=False), encoding="utf-8")
            (job / "06-reports").mkdir(parents=True)
            (job / "06-reports" / "local-acceptance.json").write_text('{"passed": true}\n', encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(SCRIPTS / "confirm_distillation.py"),
                "--job", str(job),
                "--confirmation-text", "确认此次蒸馏没有问题",
                "--confirmed-by", "测试审核人",
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((job / "05-ledgers" / "distillation-acceptance.json").read_text(encoding="utf-8"))
            self.assertEqual(record["resulting_status"], "正式")
            self.assertEqual(record["confirmation_text"], "确认此次蒸馏没有问题")
            self.assertEqual(record["documents"][0]["content_hash"], "a" * 64)

    def test_idempotent_publish_skips_unchanged_success(self) -> None:
        self.assertFalse(should_update_page("success", "same", "same", False))
        self.assertTrue(should_update_page("success", "old", "new", False))
        self.assertTrue(should_update_page("failed", "same", "same", False))

    def test_unified_header_contains_metadata_and_semantic_profile_once(self) -> None:
        profile = {
            "source_id": "TEST-1234567890AB",
            "source_hash": "a" * 64,
            "file_name": "示例",
            "source_url": "https://alidocs.dingtalk.com/i/nodes/TEST-1234567890AB",
            "creator_name": "创建者",
            "generated_at": "2026-07-31T10:00:00+08:00",
            "content_profile": {
                "page_type_candidate": "制度", "scenarios": ["审核"],
                "keywords": ["课程", "老师"], "summary": "说明评审口径", "core_theme": "教师质量",
                "business_objects": ["教师"], "document_role": "执行标准",
                "inputs": ["评估数据"], "actions": ["评审"], "outputs": ["结论"], "constraints": ["权限"],
            },
        }
        metadata = build_standard_metadata(profile, [])
        payload = unified_header_payload(profile, metadata, "业务分类视图")
        self.assertEqual(tuple(payload), AI_HEADER_FIELDS)
        self.assertEqual(payload["schema_version"], "kb-ai-document-v2")
        self.assertEqual(tuple(payload["metadata"]), STANDARD_METADATA_FIELDS)
        self.assertEqual(tuple(payload["distillation_profile"]), DISTILLATION_PROFILE_FIELDS)
        self.assertEqual(payload["distillation_profile"]["core_theme"], "教师质量")
        self.assertEqual(payload["distillation_profile"]["inputs"], ["评估数据"])
        rendered = unified_header_markdown(profile, metadata, "业务分类视图")
        self.assertEqual(rendered.count("## AI检索元数据（17字段与蒸馏画像）"), 1)
        self.assertNotIn("## AI检索卡", rendered)
        self.assertNotIn("## 标准元数据（17字段）", rendered)
        self.assertNotIn("## 蒸馏画像", rendered)

        body = rendered + "\n## Raw Mirror\n\n- 来源\n\n## 相关知识\n\n- 暂无\n\n## Original Content\n\n完整原文\n"
        readback = verify_content(
            {"title": "示例", "relative_path": "02-蒸馏结果（按文档类型）/制度/示例.md", "is_index": False},
            body,
            body,
            False,
            "示例",
        )
        self.assertTrue(readback["passed"], readback["missing"])

    def test_discover_pages_includes_root_ai_docs_and_view_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            preview = Path(temporary)
            for name in ("00-AI问答与检索入口.md", "00-AI知识库地图.md"):
                (preview / name).write_text(f"# {name}\n", encoding="utf-8")
            for view in ("01-原文镜像（按原目录）", "02-蒸馏结果（按文档类型）"):
                root = preview / view
                root.mkdir()
                (root / "目录索引.md").write_text("# 目录索引\n", encoding="utf-8")
            pages = discover_pages(preview)
            self.assertEqual(len(pages), 4)
            root_pages = [page for page in pages if page.relative_directory == "."]
            self.assertEqual(len(root_pages), 2)
            self.assertTrue(all(page.is_index for page in pages))

    def test_markdown_chunking_round_trip(self) -> None:
        text = ("## 标题\n\n" + "一段内容。" * 800 + "\n\n") * 4
        chunks = split_markdown_at_block_boundaries(text, max_characters=9000)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 9000 for chunk in chunks))

    def test_pending_l3_section_is_not_in_formal_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            preview = Path(temporary)
            source = preview / "01-原文镜像（按原目录）" / "示例.md"
            source.parent.mkdir(parents=True)
            text = (
                "# 示例\n\n"
                "> 本页为知识库蒸馏审核预览（原目录视图）；只有任务配置提供最终写回目录后才会发布。\n\n"
                "## 相关知识\n\n- 已确认关系\n\n"
                "## 待业务确认的相关知识（确认前不会发布）\n\n- 不应发布\n\n"
                "## Original Content\n\n完整原文\n"
            )
            source.write_text(text, encoding="utf-8")
            page = PublicationPage(
                relative_path="01-原文镜像（按原目录）/示例.md",
                source_path=source,
                relative_directory="01-原文镜像（按原目录）",
                title="示例",
                stable_id="TEST-1234567890AB",
                size_bytes=len(text.encode()),
                source_sha256=sha256_bytes(text.encode()),
                is_index=False,
            )
            rendered, unresolved = render_page(page, preview_root=preview, state={"documents": {}})
            self.assertNotIn("待业务确认的相关知识", rendered)
            self.assertNotIn("不应发布", rendered)
            self.assertIn("完整原文", rendered)
            self.assertEqual(unresolved, [])

    def test_directory_index_readback_uses_title_and_body_anchor(self) -> None:
        # publisher writes the H1 as the DingTalk document name; rendered_path
        # contains only the body that is sent to doc create/update.
        sent = "> 当前层级包含 24 份文档。\n\n## 当前文件夹文档\n\n- 示例\n"
        received = "> 当前层级包含 24 份文档。\n\n## 当前文件夹文档\n\n- 示例\n"
        result = verify_content(
            {
                "title": "方案｜目录索引",
                "relative_path": "02-蒸馏结果（按文档类型）/方案/目录索引.md",
                "is_index": True,
            },
            sent,
            received,
            False,
            "方案｜目录索引",
        )
        self.assertTrue(result["passed"], result["missing"])


class PackageIntegrityTests(unittest.TestCase):
    def test_schemas_are_valid_json(self) -> None:
        for path in (SKILL_ROOT / "schemas").glob("*.json"):
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_no_external_project_or_user_hardcoding(self) -> None:
        forbidden = (
            re.compile(r"/Users/[^/<\s]+"),
            re.compile(r"distill-job/scripts/extract_all\.py"),
            re.compile("具体业务知识库名称"),
        )
        violations: list[str] = []
        for path in [SKILL_ROOT / "SKILL.md", *(SKILL_ROOT / "scripts").glob("*.py")]:
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in forbidden:
                if pattern.search(text):
                    violations.append(f"{path.name}:{pattern.pattern}")
        self.assertEqual(violations, [])

    def test_mandatory_resources_exist(self) -> None:
        required = [
            SKILL_ROOT / "SKILL.md",
            SKILL_ROOT / "agents" / "openai.yaml",
            SKILL_ROOT / "scripts" / "extract_all.py",
            SKILL_ROOT / "scripts" / "preflight.py",
            SKILL_ROOT / "scripts" / "build_delivery_summary.py",
            SKILL_ROOT / "scripts" / "enrich_creators.py",
            SKILL_ROOT / "scripts" / "confirm_distillation.py",
            SKILL_ROOT / "assets" / "task-config.yaml",
            SKILL_ROOT / "assets" / "ai-query-entry.md",
            SKILL_ROOT / "assets" / "ai-knowledge-map.md",
            SKILL_ROOT / "assets" / "delivery-summary.md",
            SKILL_ROOT / "schemas" / "ai-document-header-v2.json",
            SKILL_ROOT / "references" / "raw-admission-standard.md",
            SKILL_ROOT / "references" / "08-全流程验收清单.md",
            SKILL_ROOT / "references" / "12-执行结果与下一步交付规范.md",
        ]
        self.assertEqual([str(path) for path in required if not path.exists()], [])


if __name__ == "__main__":
    unittest.main()
