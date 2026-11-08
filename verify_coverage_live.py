"""
长文档覆盖度 —— 真实模型验证（一次性脚本，可重复运行）

单元测试只能证明「切块/预算/声明」的逻辑正确，证明不了两件事：
  1. 真实模型拿到「未直接引用部分」的原文后，压出来的要点是否真的来自原文；
  2. 步骤 1 只放节选时，声明里的字数是否和实际收到的字数一致。

用法：
    .venv/Scripts/python.exe verify_coverage_live.py
"""
import sys
import time

from backend.agents.performance_agent import PerformanceAgent

# 只出现在中段（会被预算裁掉）的关键事实，用来验证「摘要是否真的读到了那一部分」
MIDDLE_FACTS = ["上浮12%", "监理日志缺3次", "42平方米"]


def build_long_document(pages: int = 15, filler: int = 2600) -> str:
    blocks = []
    for page in range(1, pages + 1):
        if page == 8:
            body = (
                "【第三方审计发现】材料单价较中标价上浮 12%，涉及金额 186 万元；"
                "监理日志缺 3 次，时段为 2025 年 3 月至 5 月；项目部档案室面积 42 平方米，"
                "不满足档案存放要求。以上问题已形成整改清单，责任主体为项目办。"
            )
        else:
            body = (f"第{page}页正文：本节记录项目实施过程中的常规管理事项与进度说明。"
                    "工作按计划推进，未发现重大异常。") * (filler // 40)
        blocks.append(f"【PDF 第{page}页】\n{body}")
    return "\n\n".join(blocks)


def main() -> int:
    text = build_long_document()
    print(f"资料长度：{len(text)} 字 / 15 页")

    agent = PerformanceAgent(
        session_id="coverage-live-check",
        model_name="deepseek-chat",
        temperature=0.3,
        top_p=0.9,
        use_rag=False,
        comparison_mode="single",
        temperature_profile="normal",
    )

    calls = []
    original_invoke = agent._invoke_llm

    def counting_invoke(messages, step_number, state, timeout=300, stream=None):
        started = time.perf_counter()
        reply = original_invoke(messages, step_number, state, timeout=timeout, stream=stream)
        is_digest = isinstance(messages[0], dict)
        prompt = messages[-1]["content"] if is_digest else getattr(messages[-1], "content", "")
        calls.append({
            "kind": "digest" if is_digest else "step",
            "step": step_number,
            "stream": stream,
            "chars": len(prompt or ""),
            "prompt": prompt or "",
            "seconds": round(time.perf_counter() - started, 1),
            "reply": reply,
        })
        return reply

    agent._invoke_llm = counting_invoke

    state = {
        "session_id": "coverage-live-check",
        "project_name": "长文档覆盖度验证项目",
        "project_type": "项目支出",
        "raw_text": text,
        "all_raw_texts": {"长报告.pdf": text},
        "completed_steps": [],
        "step_results": {},
        "warnings": [],
        "step_control": {"comparison_mode": "single"},
    }

    print("\n=== 步骤1（节选 8000 字）与步骤2（预算 12000 字）的变量 ===")
    v1 = agent._build_human_vars(state, 1)
    v2 = agent._build_human_vars(state, 2)

    plan = state["document_coverage"]
    print(f"总字数 {plan['total_chars']}｜预算内可选 {plan['used_chars']}｜未直接引用 {plan['omitted_chars']}"
          f"｜覆盖 {plan['used_ratio'] * 100:.1f}%｜模式 {plan['mode']}｜切块 {plan['chunks']}")
    print(f"步骤1 实际收到 {len(v1['raw_text_preview'])} 字；步骤2 实际收到 {len(v2['raw_text_full'])} 字")
    print(f"\n[步骤1 声明]\n{v1['document_coverage']}")
    print(f"\n[步骤2 声明]\n{v2['document_coverage']}")

    digest_calls = [c for c in calls if c["kind"] == "digest"]
    print(f"\n=== 未引用部分压缩：{len(digest_calls)} 次调用，共 {sum(c['seconds'] for c in digest_calls):.1f} 秒 ===")
    print(v1["omitted_digest"] or "（无摘要）")

    digest = v1["omitted_digest"]
    hits = [fact for fact in MIDDLE_FACTS if fact.replace(" ", "") in digest.replace(" ", "")]
    print(f"\n中段关键事实命中：{len(hits)}/{len(MIDDLE_FACTS)} -> {hits}")

    print("\n=== 真实执行步骤1（验证最终提示词里确实带上了声明与摘要）===")
    before = len(calls)
    content = agent._execute_step(state, 1)
    step_call = [c for c in calls[before:] if c["kind"] == "step"][0]
    print(f"步骤1 输出 {len(content)} 字，用时 {step_call['seconds']} 秒，提示词 {step_call['chars']} 字")

    rendered = step_call["prompt"]
    ok_statement = "【资料覆盖度】" in rendered
    ok_digest = "未直接引用部分的要点" in rendered
    direct_part = rendered.split("未直接引用部分的要点")[0]
    ok_middle_absent = all(fact not in direct_part for fact in MIDDLE_FACTS)
    print(f"提示词含覆盖度声明：{ok_statement}｜含要点摘要：{ok_digest}｜直接引用部分不含中段事实：{ok_middle_absent}")

    failures = []
    if not hits:
        failures.append("摘要未命中任何中段关键事实")
    if not ok_statement or not ok_digest:
        failures.append("最终提示词缺少覆盖度声明或要点摘要")
    if not ok_middle_absent:
        failures.append("中段事实出现在直接引用部分（说明预算没生效）")
    if len(v1["raw_text_preview"]) > agent.DOC_PREVIEW_BUDGET:
        failures.append("步骤1 节选超过预算")
    if str(len(v1["raw_text_preview"])) not in v1["document_coverage"]:
        failures.append("步骤1 声明字数与实际收到字数不一致")

    print("\n" + ("= 全部检查通过" if not failures else "= 存在问题：" + "；".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
