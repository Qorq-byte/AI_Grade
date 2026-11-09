"""状态备份的紧凑编码（批次 25 = C3）。

背景：`state_backups/*.json` 每完成一步就整份重写一遍，实测 36 个真实会话共 22.1 MB，
其中 `step_results` 占 64%，而它的 83% 是可证明的重复——
`raw_outputs["default"]` 与 `content` 逐字相同（47%，且全项目无人读），
`content` 与独立状态字段逐字相同（37%，步骤 8 的正文就是 `experience_summary`）。

这里锁住五件事：
1. 逐字重复的正文只落盘一次，读回来仍是全文（无损）；
2. 逐字**不**相同的正文必须原样保留 —— 不做任何模糊合并；
3. 多模型对比的 `raw_outputs` 不能被误删；
4. 老格式文件照常可读，且下一次写入就自动变成紧凑格式；
5. 报告拼装读到的是「字段的当前值」，不是落盘那一刻的旧快照。
"""
import json
from pathlib import Path

import pytest

from backend.agents import performance_agent
from backend.agents.performance_agent import PerformanceAgent
from backend.routes.agent_routes import _assemble_report_from_backup_steps
from backend.services import state_backup

#: 用一段够长的中文正文，让重复量在字节上看得出来（也顺带覆盖非 ASCII 不转义）
BODY = "本项目资金使用规范，支出结构合理，绩效目标基本实现。" * 40


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    target = tmp_path / "backups"
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", target)
    return target


def _step(content, number, **extra):
    """造一个与 `_record_step_result` 同形状的步骤结果。"""
    return {
        "step_number": number,
        "step_name": f"步骤{number}",
        "status": "completed",
        "content": content,
        "raw_outputs": {"default": content},
        "comparison_results": None,
        "selected_model": "test-model",
        "version": 1,
        "created_at": "2026-01-01 10:00:00",
        "updated_at": "2026-01-01 10:00:00",
        **extra,
    }


def _raw(backup_dir: Path, session_id: str) -> str:
    return (backup_dir / f"{session_id}.json").read_text(encoding="utf-8")


def test_body_is_written_once_when_it_matches_an_independent_field(backup_dir):
    """步骤 8 的正文与 experience_summary 是同一段话：磁盘上只该出现一次。"""
    PerformanceAgent.update_state_backup(
        "dup", {"experience_summary": BODY, "step_results": {"8": _step(BODY, 8)}}
    )

    raw = _raw(backup_dir, "dup")
    assert raw.count(BODY) == 1, "同一段正文在磁盘上出现了多份"

    on_disk = json.loads(raw)["step_results"]["8"]
    assert on_disk["content_ref"] == "experience_summary"
    assert "content" not in on_disk


def test_loading_restores_the_full_body(backup_dir):
    """磁盘上是引用，读回来必须是全文 —— 调用方不该感知编码。"""
    PerformanceAgent.update_state_backup(
        "dup", {"experience_summary": BODY, "step_results": {"8": _step(BODY, 8)}}
    )

    loaded = PerformanceAgent.load_state_backup("dup")

    assert loaded["step_results"]["8"]["content"] == BODY
    # 引用与预览是编码细节，不该泄进业务字段
    assert "content_ref" not in loaded["step_results"]["8"]
    assert "content_preview" not in loaded["step_results"]["8"]


def test_raw_outputs_is_dropped_when_it_mirrors_the_body(backup_dir):
    """`raw_outputs["default"]` 就是 content 的副本，且无人读取。"""
    PerformanceAgent.update_state_backup(
        "mirror", {"basic_info": BODY, "step_results": {"12": _step(BODY, 12)}}
    )

    assert "raw_outputs" not in json.loads(_raw(backup_dir, "mirror"))["step_results"]["12"]
    assert PerformanceAgent.load_state_backup("mirror")["step_results"]["12"]["raw_outputs"] == {
        "default": BODY
    }


def test_divergent_body_is_kept_verbatim(backup_dir):
    """只要有一个字符不同就必须原样两存 —— 无损是硬约束。"""
    field_body = "字段里的正文甲" * 60
    step_body = "步骤里的正文乙" * 60
    PerformanceAgent.update_state_backup(
        "diverge", {"experience_summary": field_body, "step_results": {"8": _step(step_body, 8)}}
    )

    raw = _raw(backup_dir, "diverge")
    assert raw.count(field_body) == 1
    assert raw.count(step_body) == 1
    assert json.loads(raw)["step_results"]["8"].get("content") == step_body

    loaded = PerformanceAgent.load_state_backup("diverge")
    assert loaded["step_results"]["8"]["content"] == step_body


def test_multi_model_raw_outputs_survives_compaction(backup_dir):
    """对比模式下 raw_outputs 存的是各模型的原始输出，不是 content 的副本。"""
    item = _step(BODY, 8)
    item["raw_outputs"] = {"model-a": BODY, "model-b": "另一份输出"}
    PerformanceAgent.update_state_backup(
        "multi", {"experience_summary": BODY, "step_results": {"8": item}}
    )

    on_disk = json.loads(_raw(backup_dir, "multi"))["step_results"]["8"]
    assert on_disk["raw_outputs"] == {"model-a": BODY, "model-b": "另一份输出"}
    assert "content" not in on_disk, "正文仍应与字段合并存储"


def test_referenced_field_that_disappears_falls_back_to_the_preview(backup_dir):
    """字段被清空时不能返回空正文；200 字预览是最后一道兜底。

    走真实写入路径产生引用与预览，再把字段从文件里抹掉 —— 手工拼一份文件测不到
    「写的时候到底存没存预览」这件事（变异检验里正是这一条漏过网）。
    """
    PerformanceAgent.update_state_backup(
        "stale", {"experience_summary": BODY, "step_results": {"8": _step(BODY, 8)}}
    )
    broken = json.loads(_raw(backup_dir, "stale"))
    assert broken.pop("experience_summary", None) == BODY
    (backup_dir / "stale.json").write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

    loaded = PerformanceAgent.load_state_backup("stale")

    assert loaded["step_results"]["8"]["content"] == BODY[:state_backup.CONTENT_PREVIEW_CHARS]


def test_legacy_backup_still_reads_and_gets_compacted_on_the_next_write(backup_dir):
    """改造前写下的文件（content + raw_outputs + indent=2）必须照常读，并在下次写入时收缩。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "session_id": "legacy",
        "project_name": "老会话",
        "experience_summary": BODY,
        "step_results": {"8": _step(BODY, 8)},
        "final_report": "# 报告\n" + BODY,
    }
    path = backup_dir / "legacy.json"
    path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
    before = path.stat().st_size

    assert PerformanceAgent.load_state_backup("legacy")["step_results"]["8"]["content"] == BODY

    PerformanceAgent.update_state_backup("legacy", {"review_steps": []})

    assert path.stat().st_size < before
    assert PerformanceAgent.load_state_backup("legacy")["step_results"]["8"]["content"] == BODY


def test_compaction_round_trip_is_lossless(backup_dir):
    """任意形状（重复的、不重复的、多模型的）过一遍 compact→hydrate 必须原样回来。"""
    original = {
        "session_id": "round",
        "experience_summary": BODY,
        "final_report": "# 报告\n" + BODY,
        "step_results": {
            "8": _step(BODY, 8),
            "9": _step("独立正文" * 50, 9),
            "14": _step("# 报告\n" + BODY, 14),
        },
    }

    assert state_backup.hydrate(state_backup.compact(original)) == original


def test_field_only_edit_keeps_the_saved_step_body_as_a_snapshot(backup_dir):
    """只改字段（后台改文案/保存报告）时，步骤正文保持落盘时的快照。

    这是**有意**的选择：合并的前提是两处逐字相同，一旦字段被单独改过就重新分存。
    宁可多存一份历史正文，也不静默改写「模型当初写了什么」。
    正常编辑路径（`_apply_step_edit`）会同时写字段和步骤正文，那时两者重新相等、
    自动退回引用存储 —— 见下一条断言。
    """
    old, new = "旧的问题分析" * 60, "新的问题分析" * 60
    PerformanceAgent.update_state_backup(
        "report", {"problems_analysis": old, "step_results": {"9": _step(old, 9)}}
    )
    assert "content" not in json.loads(_raw(backup_dir, "report"))["step_results"]["9"]

    PerformanceAgent.update_state_backup("report", {"problems_analysis": new})

    loaded = PerformanceAgent.load_state_backup("report")
    assert loaded["problems_analysis"] == new
    assert loaded["step_results"]["9"]["content"] == old, "快照被静默改写了"
    assert old in _assemble_report_from_backup_steps(loaded)


def test_synchronized_edit_returns_to_reference_storage(backup_dir):
    """字段与步骤正文一起改（真实编辑路径）时，两处重新相等，磁盘上又只剩一份。"""
    old, new = "旧的改进建议" * 60, "新的改进建议" * 60
    PerformanceAgent.update_state_backup(
        "sync", {"suggestions": old, "step_results": {"10": _step(old, 10)}}
    )

    PerformanceAgent.update_state_backup(
        "sync", {"suggestions": new, "step_results": {"10": _step(new, 10)}}
    )

    raw = _raw(backup_dir, "sync")
    assert raw.count(old) == 0
    assert raw.count(new) == 1
    assert PerformanceAgent.load_state_backup("sync")["step_results"]["10"]["content"] == new


def test_a_realistic_session_shrinks_by_more_than_half(backup_dir):
    """14 步各写一段正文，且每段都与独立字段重复：紧凑后应少于一半。"""
    fields = {
        1: "cost_list", 2: "effective_data", 6: "evaluation_table", 7: "indicator_analysis",
        8: "experience_summary", 9: "problems_analysis", 10: "suggestions",
        11: "comprehensive_evaluation", 12: "basic_info", 13: "work_introduction",
        14: "final_report",
    }
    payload = {"project_name": "体积用例", "step_results": {}}
    for step, field in fields.items():
        body = f"第{step}步正文。" * 400
        payload[field] = body
        payload["step_results"][str(step)] = _step(body, step)

    naive = len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    PerformanceAgent.update_state_backup("volume", payload)
    compacted = (backup_dir / "volume.json").stat().st_size

    assert compacted / naive < 0.5, f"紧凑比 {compacted / naive:.2f}，重复没有被消掉"

    loaded = PerformanceAgent.load_state_backup("volume")
    for step, field in fields.items():
        assert loaded["step_results"][str(step)]["content"] == loaded[field]


def test_migration_script_rewrites_without_changing_meaning(backup_dir, tmp_path):
    """存量迁移脚本：体积变小，语义不变。"""
    from compact_state_backups import compact_file

    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / "migrate.json"
    path.write_text(json.dumps({
        "session_id": "migrate",
        "experience_summary": BODY,
        "step_results": {"8": _step(BODY, 8)},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    before_bytes = path.stat().st_size
    before_state = state_backup.hydrate(json.loads(path.read_text(encoding="utf-8")))

    row = compact_file(path, apply=True)

    assert row["status"] == "已重写", row
    assert path.stat().st_size < before_bytes
    assert state_backup.hydrate(json.loads(path.read_text(encoding="utf-8"))) == before_state
