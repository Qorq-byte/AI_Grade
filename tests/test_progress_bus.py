"""progress_bus 单元测试：增量队列、补发缓冲、消费者门控与代理语义。"""
import importlib

from backend.services import progress_bus


def setup_function():
    progress_bus.PROGRESS.clear()


def test_delta_requires_active_stream():
    progress_bus.begin("s1")
    progress_bus.push_delta("s1", 3, "无人收听时丢弃")
    assert progress_bus.drain_deltas("s1") == []

    progress_bus.attach_consumer("s1")
    progress_bus.push_delta("s1", 3, "指标")
    progress_bus.push_delta("s1", 3, "体系")
    assert progress_bus.drain_deltas("s1") == [(3, "指标"), (3, "体系")]
    assert progress_bus.drain_deltas("s1") == []


def test_consumer_count_gates_streaming():
    progress_bus.begin("s2")
    assert progress_bus.has_consumer("s2") is False
    progress_bus.attach_consumer("s2")
    assert progress_bus.has_consumer("s2") is True
    progress_bus.detach_consumer("s2")
    assert progress_bus.has_consumer("s2") is False


def test_unknown_session_drops_delta():
    progress_bus.push_delta("never-started", 1, "x")
    assert progress_bus.drain_deltas("never-started") == []


def test_frames_since_returns_only_newer_events():
    progress_bus.begin("s3")
    for seq in range(1, 6):
        progress_bus.record_frame("s3", seq, f"id: {seq}\n")
    assert [s for s, _ in progress_bus.frames_since("s3", 0)] == [1, 2, 3, 4, 5]
    assert [s for s, _ in progress_bus.frames_since("s3", 3)] == [4, 5]
    assert progress_bus.last_seq("s3") == 0


def test_seq_is_monotonic_per_session():
    progress_bus.begin("s4")
    seqs = [progress_bus.next_seq("s4") for _ in range(3)]
    assert seqs == [1, 2, 3]
    progress_bus.begin("s4")
    assert progress_bus.next_seq("s4") == 1


def test_proxy_survives_module_reload():
    """模块 reload 会重建 PROGRESS，代理必须仍指向当前实例。"""
    progress_bus.PROXY["sid"] = {"status": "running"}
    reloaded = importlib.reload(progress_bus)
    try:
        assert reloaded.PROGRESS == {}
        reloaded.PROXY["sid"] = {"status": "completed"}
        assert reloaded.PROGRESS["sid"]["status"] == "completed"
        assert "sid" in reloaded.PROXY
        assert reloaded.PROXY.get("missing") is None
        reloaded.PROXY.pop("sid")
        assert "sid" not in reloaded.PROGRESS
    finally:
        importlib.reload(progress_bus)
