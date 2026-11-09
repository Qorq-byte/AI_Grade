"""批次 23（A3 lifespan 迁移）的变异测试。

锁的是四件事：应用还挂不挂 lifespan、启动/关闭还跑不跑、清理任务拿不拿得住、
关闭时有没有真的等任务收尾。每个变异体先离线 compile()。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python"
TARGET_TESTS = "tests/test_lifespan.py"
MAIN = "backend/main.py"

MUTATIONS = [
    ("lifespan 不跑启动（进去就 yield）",
     MAIN,
     """    await _on_startup()
    try:
        yield""",
     """    try:
        yield"""),
    ("lifespan 退出不跑关闭",
     MAIN,
     """    finally:
        # 用 finally：正常退出、异常退出、被取消，都要把后台任务收干净
        await _on_shutdown()""",
     """    finally:
        pass"""),
    ("用 except 代替 finally（异常路径不关闭）",
     MAIN,
     """    try:
        yield
    finally:""",
     """    try:
        yield
    except Exception:"""),
    ("应用不再挂 lifespan（on_event 时代的行为）",
     MAIN,
     """    debug=settings.DEBUG,
    lifespan=lifespan,
)""",
     """    debug=settings.DEBUG,
)"""),
    ("清理任务不持引用（丢掉 create_task 的返回值）",
     MAIN,
     """    _CLEANUP_TASK = asyncio.create_task(_cleanup_loop(), name="file-cleanup")""",
     """    asyncio.create_task(_cleanup_loop(), name="file-cleanup")"""),
    ("重复启动不去重（会多跑一条清理循环）",
     MAIN,
     """    if _CLEANUP_TASK is not None and not _CLEANUP_TASK.done():
        return _CLEANUP_TASK""",
     """    if False:
        return _CLEANUP_TASK"""),
    ("任务不命名（排查时看不出是谁）",
     MAIN,
     """    _CLEANUP_TASK = asyncio.create_task(_cleanup_loop(), name="file-cleanup")""",
     """    _CLEANUP_TASK = asyncio.create_task(_cleanup_loop())"""),
    ("关闭只 cancel 不 await（清理逻辑停在半路）",
     MAIN,
     """    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass""",
     """    task.cancel()
    try:
        pass
    except asyncio.CancelledError:
        pass"""),
    ("关闭后不清引用（下次启动拿到的还是旧任务）",
     MAIN,
     """    task, _CLEANUP_TASK = _CLEANUP_TASK, None""",
     """    task = _CLEANUP_TASK"""),
    ("关闭阶段的异常不兜（进程退出时抛给上层）",
     MAIN,
     """    try:
        await _stop_cleanup_scheduler()
    except Exception as e:  # noqa: BLE001 关闭阶段不能再把进程卡住
        logger.error(f"关闭清理定时器失败: {e}")""",
     """    await _stop_cleanup_scheduler()"""),
    ("DB 起不来也起清理任务",
     MAIN,
     """        _start_cleanup_scheduler()

    except Exception as e:
        logger.error(f"数据库初始化失败: {str(e)}")""",
     """        pass

    except Exception as e:
        logger.error(f"数据库初始化失败: {str(e)}")
    _start_cleanup_scheduler()"""),
    ("启动不再建表（init_db 被删）",
     MAIN,
     """        init_db()
        logger.info("数据库初始化成功")""",
     """        logger.info("数据库初始化成功")"""),
]

paths = sorted({path for _, path, _, _ in MUTATIONS})
originals = {path: Path(path).read_text(encoding="utf-8") for path in paths}
failures = []


def compile_ok(path):
    try:
        compile(Path(path).read_text(encoding="utf-8"), path, "exec")
        return True
    except SyntaxError as exc:
        print(f"  变异体语法错误，跳过：{exc}")
        return False


try:
    for name, path, old, new in MUTATIONS:
        if old not in originals[path]:
            failures.append(f"{name}: 变异点未找到")
            print(f"跳过  {name}   （变异点未找到）")
            continue
        Path(path).write_text(originals[path].replace(old, new, 1), encoding="utf-8")
        if not compile_ok(path):
            Path(path).write_text(originals[path], encoding="utf-8")
            continue
        result = subprocess.run(
            [PY, "-m", "pytest", *TARGET_TESTS.split(), "-q", "--no-header",
             "-p", "no:cacheprovider"],
            capture_output=True, text=True)
        line = [item for item in result.stdout.splitlines() if "passed" in item or "failed" in item]
        caught = result.returncode != 0
        print(f"{'抓住' if caught else '漏掉'}  {name}   {line[-1] if line else ''}")
        if not caught:
            failures.append(name)
        Path(path).write_text(originals[path], encoding="utf-8")
finally:
    for path, text in originals.items():
        Path(path).write_text(text, encoding="utf-8")

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
