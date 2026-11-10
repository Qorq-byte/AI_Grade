"""批次 31 = B7：模型调用的分级超时与降级链。

背景：改前所有步骤都写死 300 s、单模型、单次调用。一次 429 或网络抖动就让整步失败，
用户看到「步骤失败，请检查模型配置」——查不出是配额、超时还是配置错。
短步骤失败时要干等 5 分钟才报错。

这里锁四件事：
1. 超时按步骤分级（长文 300、其余 120），显式传参优先；
2. 只有 transient 错误（429/超时/5xx/连接）才换模型，400/401/403 直接抛；
3. 每次尝试都记账（成功也记），上限 200 条；
4. 降级成功必须写一条 warning —— **换模型会改变文字风格与判断**，读者有权知道。
"""
import json
from types import SimpleNamespace

import pytest

from backend.agents import performance_agent as pa


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    """测试里不真的 sleep：退避基数为 0（行为仍走同一条代码路径）。"""
    monkeypatch.setenv("LLM_RETRY_BACKOFF_SECONDS", "0")


# ═══════════════════════════════════════════════════════
#  分级超时
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("step", [5, 6, 7, 9, 10])
def test_long_form_steps_get_300_seconds(step):
    assert pa.step_timeout(step) == 300


@pytest.mark.parametrize("step", [1, 2, 3, 4, 8, 11, 12, 13, 14])
def test_other_steps_get_120_seconds(step):
    assert pa.step_timeout(step) == 120


def test_step_timeout_tolerates_junk_input():
    """状态里的步骤号可能是字符串（备份 JSON），不该因此炸掉。"""
    assert pa.step_timeout("7") == 300
    assert pa.step_timeout(None) == 120


# ═══════════════════════════════════════════════════════
#  错误分类：认不出来的当 permanent
# ═══════════════════════════════════════════════════════

class _StatusError(Exception):
    def __init__(self, status_code, message="boom"):
        super().__init__(message)
        self.status_code = status_code


class _WrappedError(Exception):
    """有些 SDK 把状态码埋在 `.response.status_code` 里。"""

    def __init__(self, status_code):
        super().__init__("wrapped")
        self.response = SimpleNamespace(status_code=status_code)


@pytest.mark.parametrize("exc", [
    _StatusError(429), _StatusError(500), _StatusError(503), _StatusError(408),
    _WrappedError(502), TimeoutError("slow"), ConnectionError("reset"),
])
def test_transient_errors(exc):
    assert pa.classify_llm_error(exc) == "transient"


@pytest.mark.parametrize("exc", [
    _StatusError(400), _StatusError(401), _StatusError(403), _StatusError(404),
    _WrappedError(422), ValueError("bad config"), RuntimeError("unexpected"),
])
def test_permanent_errors(exc):
    assert pa.classify_llm_error(exc) == "permanent"


# ═══════════════════════════════════════════════════════
#  降级链
# ═══════════════════════════════════════════════════════

class _Resp:
    def __init__(self, content):
        self.content = content
        self.response_metadata = {}


class _FakeLLM:
    """最小可用的假客户端：只实现 invoke，且**不带 stream 属性**（避免走流式）。"""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def invoke(self, messages, config=None):
        self.calls.append(config)
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return _Resp(self.behaviour)


def _agent(monkeypatch, behaviours, primary="primary-model", fallbacks=None):
    """造一个只替换模型层的 agent：`behaviours` 是 {model: 内容或异常}。"""
    instance = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    instance.session_id = "resilience-fixture"
    instance.model_name = primary
    instance.temperature = 0.7
    instance.top_p = 0.9

    fakes = {name: _FakeLLM(value) for name, value in behaviours.items()}
    instance.base_agent = SimpleNamespace(
        llm=fakes.get(primary), model_name=primary, max_tokens=1024)

    def fake_llm_for_step(step_number, model_name=None):
        return fakes.get(model_name or primary)

    monkeypatch.setattr(instance, "_llm_for_step", fake_llm_for_step)
    state = {"session_id": "resilience-fixture", "warnings": []}
    if fallbacks is not None:
        state["system_config"] = {"fallback_models": fallbacks}
    return instance, state, fakes


def test_transient_failure_falls_back_and_returns_the_second_answer(monkeypatch):
    agent, state, fakes = _agent(
        monkeypatch,
        {"primary-model": _StatusError(429), "backup-model": "备选模型写的内容"},
        fallbacks=["backup-model"],
    )

    content = agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state)

    assert content == "备选模型写的内容"
    # 批次 32 起先重试同一模型一次，仍失败才换备选 → 主模型 2 次、备选 1 次
    assert len(fakes["primary-model"].calls) == 2
    assert len(fakes["backup-model"].calls) == 1
    attempts = state["llm_attempts"]
    assert [item["model"] for item in attempts] == [
        "primary-model", "primary-model", "backup-model"]
    assert [item["outcome"] for item in attempts] == ["error", "error", "ok"]
    assert attempts[0]["detail"].startswith("transient")
    # 降级必须留痕：换模型会改变文字风格与判断
    assert any("备选模型 backup-model" in w for w in state["warnings"])


class _FlakyLLM:
    """前 N 次抛 429，之后成功 —— 用来验证「先重试同一模型」。"""

    def __init__(self, failures, content="重试后的正文"):
        self.failures = failures
        self.content = content
        self.calls = 0

    def invoke(self, messages, config=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise _StatusError(429)
        return _Resp(self.content)


def test_retry_saves_the_day_without_switching_models(monkeypatch):
    """429 常常 1-2 秒就好：重试同一模型成功时，不换模型、不写降级警告。"""
    agent, state, _ = _agent(monkeypatch, {}, fallbacks=["backup-model"])
    flaky = _FlakyLLM(failures=1)
    backup = _FakeLLM("不该被调用")
    fakes = {"primary-model": flaky, "backup-model": backup}
    monkeypatch.setattr(agent, "_llm_for_step",
                        lambda step, model=None: fakes[model or "primary-model"])

    content = agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state)

    assert content == "重试后的正文"
    assert flaky.calls == 2
    assert backup.calls == []
    assert [item["outcome"] for item in state["llm_attempts"]] == ["error", "ok"]
    assert not [w for w in state["warnings"] if "备选模型" in w]


def test_retry_count_is_configurable(monkeypatch):
    """重试次数为 0 时行为回到批次 31：失败立刻换模型。"""
    monkeypatch.setattr(pa, "LLM_RETRY_ATTEMPTS", 0)
    agent, state, fakes = _agent(
        monkeypatch,
        {"primary-model": _StatusError(429), "backup-model": "备选"},
        fallbacks=["backup-model"],
    )

    content = agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state)

    assert content == "备选"
    assert len(fakes["primary-model"].calls) == 1
    assert [item["model"] for item in state["llm_attempts"]] == [
        "primary-model", "backup-model"]


def test_backoff_is_linear_and_env_configurable(monkeypatch):
    monkeypatch.delenv("LLM_RETRY_BACKOFF_SECONDS", raising=False)
    assert pa.retry_backoff(1) == 2.0
    assert pa.retry_backoff(2) == 4.0
    monkeypatch.setenv("LLM_RETRY_BACKOFF_SECONDS", "0.5")
    assert pa.retry_backoff(2) == 1.0
    monkeypatch.setenv("LLM_RETRY_BACKOFF_SECONDS", "junk")
    assert pa.retry_backoff(1) == 2.0


def test_permanent_failure_does_not_burn_the_backup_model(monkeypatch):
    agent, state, fakes = _agent(
        monkeypatch,
        {"primary-model": _StatusError(401), "backup-model": "不该被调用"},
        fallbacks=["backup-model"],
    )

    with pytest.raises(_StatusError):
        agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state)

    assert fakes["backup-model"].calls == []
    assert len(state["llm_attempts"]) == 1
    assert not state["warnings"]


def test_unknown_exception_is_not_retried_on_another_model(monkeypatch):
    """认不出来的异常按 permanent 处理：宁可少试一次，也不要拿编程错误烧两遍钱。"""
    agent, state, fakes = _agent(
        monkeypatch,
        {"primary-model": RuntimeError("代码 bug"), "backup-model": "不该被调用"},
        fallbacks=["backup-model"],
    )

    with pytest.raises(RuntimeError):
        agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state)

    assert fakes["backup-model"].calls == []


def test_all_models_failing_raises_the_last_error(monkeypatch):
    agent, state, _ = _agent(
        monkeypatch,
        {"primary-model": _StatusError(503), "backup-model": _StatusError(502)},
        fallbacks=["backup-model"],
    )

    with pytest.raises(_StatusError) as info:
        agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state)

    assert info.value.status_code == 502
    # 两个模型各重试一次 → 4 次尝试
    assert len(state["llm_attempts"]) == 4
    assert {item["outcome"] for item in state["llm_attempts"]} == {"error"}


def test_explicit_timeout_overrides_the_table(monkeypatch):
    agent, state, fakes = _agent(monkeypatch, {"primary-model": "ok"})

    agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state, timeout=42)

    assert fakes["primary-model"].calls[0]["timeout"] == 42


def test_default_timeout_comes_from_the_table(monkeypatch):
    agent, state, fakes = _agent(monkeypatch, {"primary-model": "ok"})

    agent._invoke_llm([{"role": "user", "content": "hi"}], 12, state)

    assert fakes["primary-model"].calls[0]["timeout"] == 120


# ═══════════════════════════════════════════════════════
#  备选链的组成
# ═══════════════════════════════════════════════════════

def test_chain_dedupes_primary_and_caps_at_three(monkeypatch):
    agent, state, _ = _agent(monkeypatch, {"primary-model": "ok"},
                             fallbacks=["primary-model", "b", "c", "d"])

    assert agent._model_chain(state) == ["primary-model", "b", "c"]


def test_chain_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", " x , y ")
    agent, state, _ = _agent(monkeypatch, {"primary-model": "ok"})

    assert agent._model_chain(state) == ["primary-model", "x", "y"]


def test_no_model_at_all_raises_a_clear_error(monkeypatch):
    agent, state, _ = _agent(monkeypatch, {}, primary="")

    with pytest.raises(RuntimeError, match="没有可用模型"):
        agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state)


# ═══════════════════════════════════════════════════════
#  台账
# ═══════════════════════════════════════════════════════

def test_attempt_ledger_keeps_only_the_recent_200(monkeypatch):
    agent, state, _ = _agent(monkeypatch, {"primary-model": "ok"})
    state["llm_attempts"] = [{"step": 1, "model": "old", "attempt": 1,
                              "latency_ms": 0, "outcome": "ok", "detail": ""}] * 250

    agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state)

    assert len(state["llm_attempts"]) == 200
    assert state["llm_attempts"][-1]["step"] == 9


def test_attempts_accumulate_across_steps(monkeypatch):
    """台账必须跨步骤累积。

    第一版只写进 `state["llm_attempts"]`：LangGraph 每个节点拿的是状态副本，
    合并时后写覆盖前写 —— 真实一轮 14 步跑完只剩最后一步的 1 条（实测）。
    改成存在实例上再镜像给 state。
    """
    agent, state1, _ = _agent(monkeypatch, {"primary-model": "第一段"})

    agent._invoke_llm([{"role": "user", "content": "a"}], 9, state1)
    state2 = {"session_id": "resilience-fixture", "warnings": []}
    agent._invoke_llm([{"role": "user", "content": "b"}], 10, state2)

    assert [item["step"] for item in state2["llm_attempts"]] == [9, 10]


def test_attempts_from_backup_are_continued(monkeypatch):
    """续跑：备份里已有的尝试要接上，而不是从零覆盖。"""
    agent, state, _ = _agent(monkeypatch, {"primary-model": "ok"})
    state["llm_attempts"] = [{"step": 1, "model": "old", "attempt": 1,
                              "latency_ms": 5, "outcome": "ok", "detail": ""}]

    agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state)

    assert [item["step"] for item in state["llm_attempts"]] == [1, 9]


def test_fallback_hint_is_written_only_once(monkeypatch):
    """同一步反复降级（比如重试整步）不该在 warnings 里堆一模一样的话。"""
    agent, state, _ = _agent(
        monkeypatch,
        {"primary-model": _StatusError(429), "backup-model": "ok"},
        fallbacks=["backup-model"],
    )

    agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state)
    agent._invoke_llm([{"role": "user", "content": "hi"}], 9, state)

    hits = [w for w in state["warnings"] if "备选模型 backup-model" in w]
    assert len(hits) == 1


def test_attempts_are_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "STATE_BACKUP_DIR", tmp_path)
    state = {"session_id": "persist-attempts", "warnings": [],
             "llm_attempts": [{"step": 9, "model": "m", "attempt": 1,
                               "latency_ms": 12, "outcome": "ok", "detail": ""}]}
    state["step_control"] = {}

    agent, _, _ = _agent(monkeypatch, {"primary-model": "ok"})
    agent.session_id = "persist-attempts"
    agent._persist_state_backup(state)

    payload = json.loads((tmp_path / "persist-attempts.json").read_text(encoding="utf-8"))
    assert payload["llm_attempts"][0]["latency_ms"] == 12
