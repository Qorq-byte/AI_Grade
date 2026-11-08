"""跨进程并发探针：量化「备份文件被别的进程占住」时的写入失败次数。

`_STATE_BACKUP_LOCK` 只是进程内的 RLock，挡不住别的进程——别的 uvicorn worker、
备份软件、杀毒扫描都会拿着读句柄。这里用一个独立的「读进程」持续打开备份文件，
另一个「写进程」反复调 `update_state_backup`，对比两种设置：

  预算 0 秒（等价改前：失败即抛）  → 统计 PermissionError 次数
  预算 2 秒（改后：退避重试）      → 统计 PermissionError 次数

用法: .venv\\Scripts\\python tests/_probe_backup_race.py
"""
import multiprocessing as mp
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SESSION = "probe-race"
ITERATIONS = 400


def _writer(backup_dir: str, budget: float, iterations: int, done) -> None:
    from backend.agents import performance_agent
    from backend.agents.performance_agent import PerformanceAgent

    performance_agent.STATE_BACKUP_DIR = Path(backup_dir)
    performance_agent.STATE_BACKUP_IO_BUDGET_SEC = budget
    failures = 0
    for index in range(iterations):
        try:
            PerformanceAgent.update_state_backup(SESSION, {"probe_counter": index})
        except PermissionError:
            failures += 1
    done.put(failures)


def _reader(backup_dir: str, stop) -> None:
    path = Path(backup_dir) / f"{SESSION}.json"
    reads = 0
    while not stop.is_set():
        try:
            with path.open(encoding="utf-8") as handle:
                handle.read()
            reads += 1
        except OSError:
            pass


def _round(budget: float) -> tuple[int, int]:
    from backend.agents import performance_agent
    from backend.agents.performance_agent import PerformanceAgent

    backup_dir = tempfile.mkdtemp(prefix="backup-race-")
    original_dir = performance_agent.STATE_BACKUP_DIR
    try:
        performance_agent.STATE_BACKUP_DIR = Path(backup_dir)
        PerformanceAgent.update_state_backup(SESSION, {"probe_counter": -1})
    finally:
        performance_agent.STATE_BACKUP_DIR = original_dir

    done, stop = mp.Queue(), mp.Event()
    reader = mp.Process(target=_reader, args=(backup_dir, stop))
    writer = mp.Process(target=_writer, args=(backup_dir, budget, ITERATIONS, done))
    reader.start()
    time.sleep(0.2)          # 让读进程真的开始占用文件
    writer.start()
    failures = done.get()
    writer.join()
    stop.set()
    reader.join()
    shutil.rmtree(backup_dir, ignore_errors=True)
    return failures, ITERATIONS


if __name__ == "__main__":
    print(f"每轮 {ITERATIONS} 次写入，另一进程持续读取同一份备份\n")
    before, total = _round(budget=0.0)
    after, _ = _round(budget=2.0)
    print(f"改前（预算 0 秒，失败即抛）: {before}/{total} 次 PermissionError")
    print(f"改后（预算 2 秒，退避重试）: {after}/{total} 次 PermissionError")
    print(f"\n结论: 失败次数 {before} → {after}")
    sys.exit(0 if after == 0 else 1)
