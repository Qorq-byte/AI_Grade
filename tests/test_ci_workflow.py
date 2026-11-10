"""CI 配置的契约测试（批次 28 = C5）。

本地跑不了 GitHub Actions，能守的只有两件事：

1. **该跑的还在跑**：编译自检、pytest、node --test 三步一个都不能少；
2. **不该跑的没被加进来**：`test_all_apis` / `wf_e2e` / `frontend_e2e_check` / `rag_eval`
   需要真实 MySQL + 模型 + 8000 端口，进了 CI 就会变成随机红 ——
   **红了三次之后大家就开始忽略 CI，那比没有 CI 更糟**。

这条用例的价值不在"验证 YAML 语法"，而在于把「哪些检查不进 CI」这个**决定**写进代码，
以后有人顺手加一条 `wf_e2e.py` 时，会当场被拦下。
"""
from pathlib import Path

# 硬导入，不用 `importorskip`：缺 PyYAML 时应当**报错**，而不是静默跳过整份契约。
# 静默跳过 = 假绿，这正是本批在治的东西。PyYAML 是 `requirements-dev.txt` 里的依赖。
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

#: 必须在 CI 里跑的命令片段
REQUIRED = ("compileall", "pytest tests", "node --test")
#: 明确不许进 CI 的脚本（需要真实服务/模型，必然随机红）
FORBIDDEN = ("test_all_apis", "wf_e2e", "frontend_e2e_check", "verify_coverage_live",
             "rag_eval/run")


def _job():
    assert WORKFLOW.exists(), "CI 工作流没了"
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = data.get("jobs") or {}
    assert "test" in jobs, f"缺少 test job：{list(jobs)}"
    return data, jobs["test"]


def _run_commands(job):
    return [str(step.get("run", "")) for step in job.get("steps", [])]


def test_ci_runs_the_three_offline_checks():
    _, job = _job()
    commands = " ".join(_run_commands(job))

    for fragment in REQUIRED:
        assert fragment in commands, f"CI 少了这一步：{fragment}"


def test_ci_never_runs_checks_that_need_a_real_server_or_model():
    _, job = _job()
    commands = " ".join(_run_commands(job))

    for fragment in FORBIDDEN:
        assert fragment not in commands, (
            f"{fragment} 需要真实服务/模型，放进 CI 会变成随机红 —— 见模块文档")


def test_ci_points_at_a_throwaway_database():
    """测试全走 sqlite 夹具。CI 里把 MySQL 指到不存在的端口，能当场暴露"偷偷连真库"的用例。

    批次 28 就是靠这一条抓出 `test_startup_still_initializes_db`：它原来靠本机真有 MySQL
    才通过，注释里写着"验证启动顺序"，实际验证的是"我机器上有库"。
    """
    _, job = _job()
    env = job.get("env") or {}

    assert str(env.get("MYSQL_PORT")) == "1", f"MYSQL_PORT 应指向不存在的端口，实际 {env.get('MYSQL_PORT')}"


def test_ci_uses_the_pinned_dev_requirements():
    """CI 必须装 `requirements-dev.txt`（锁版本、不含 torch 那套推理栈）。

    装 `requirements.txt` 会把 torch / chromadb / faiss 拉进来 —— 2 GB 只为跑单测。
    """
    _, job = _job()
    install = [cmd for cmd in _run_commands(job) if "pip install" in cmd]

    assert install, "CI 里没有安装依赖的步骤"
    assert all("requirements-dev.txt" in cmd for cmd in install), install
