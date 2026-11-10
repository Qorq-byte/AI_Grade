"""Replay repository smoke scripts against an isolated loopback server and fake LLM."""
import argparse
import json
import os
import runpy
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch


@contextmanager
def isolated_mysql(binary, root):
    """Start a new MySQL data directory on loopback without reading machine configuration."""
    import pymysql

    data = root / "mysql-data"
    options = [str(binary), "--no-defaults", f"--datadir={data}", "--mysqlx=OFF"]
    with (root / "mysql-init.log").open("wb") as log:
        result = subprocess.run([*options, "--initialize-insecure"], stdout=log, stderr=subprocess.STDOUT,
                                timeout=120, check=False)
    if result.returncode:
        raise RuntimeError("MySQL initialization failed. See mysql-init.log in the test directory.")
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]

    with (root / "mysql-server.log").open("wb") as log:
        process = subprocess.Popen(
            [*options, "--bind-address=127.0.0.1", f"--port={port}", "--skip-log-bin", "--console"],
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            for _ in range(150):
                if process.poll() is not None:
                    raise RuntimeError("Isolated MySQL exited. See mysql-server.log in the test directory.")
                try:
                    connection = pymysql.connect(host="127.0.0.1", port=port, user="root", connect_timeout=1)
                except pymysql.MySQLError:
                    time.sleep(0.2)
                else:
                    with connection, connection.cursor() as cursor:
                        cursor.execute("CREATE DATABASE ai_grade_test CHARACTER SET utf8mb4")
                    break
            else:
                raise RuntimeError("Isolated MySQL did not become ready.")
            yield f"mysql+pymysql://root@127.0.0.1:{port}/ai_grade_test"
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)


def replay(root, database_url):
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    os.chdir(root)
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: None
    import requests
    import uvicorn
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.mysql import MEDIUMTEXT
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import sessionmaker
    from backend import database
    from backend.agents import base_agent, performance_agent
    from backend.agents.tools.indicator_manager import TEMPLATE_PROJECT_EXPENDITURE
    from backend.main import app
    from backend.middleware import auth_middleware
    from backend.routes import _shared_state, agent_routes, client_routes
    from backend.services.auth_service import create_admin_if_not_exists

    compiles(MEDIUMTEXT, "sqlite")(lambda element, compiler, **kwargs: "TEXT")
    auth_middleware.SECRET_KEY = "isolation-only-signing"
    performance_agent.STATE_BACKUP_DIR = root / "backups"
    agent_routes.UPLOAD_DIR = root / "uploads"
    client_routes.AVATARS_DIR = root / "avatars"
    agent_routes.UPLOAD_DIR.mkdir()
    client_routes.AVATARS_DIR.mkdir()
    agent_routes._get_templates_file = lambda: str(root / "test-templates.json")

    class FakeLLM:
        def __init__(self, *args, **kwargs):
            pass

        def invoke(self, *args, **kwargs):
            from langchain_core.messages import AIMessage
            return AIMessage(content="deterministic offline result")

    base_agent.BaseAgent._initialize_llm = lambda self: FakeLLM()
    agent_routes.ChatOpenAI = FakeLLM

    def fake_step(self, state, number):
        if number <= 3:
            time.sleep(0.34)
        if number == 3:
            return json.dumps(TEMPLATE_PROJECT_EXPENDITURE, ensure_ascii=False)
        return f"## Step {number}\n\nDeterministic fixture for the workflow report and regression checks."

    performance_agent.PerformanceAgent._execute_step = fake_step
    engine = create_engine(database_url)
    database.Base.metadata.create_all(engine)
    database.SessionLocal = sessionmaker(bind=engine)
    base_agent.SessionLocal = database.SessionLocal
    performance_agent.SessionLocal = database.SessionLocal
    with database.SessionLocal() as db:
        create_admin_if_not_exists(db)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    request = requests.Session.request

    def local_only(self, method, url, *args, **kwargs):
        parts = urlsplit(url)
        assert parts.scheme == "http" and parts.netloc == "127.0.0.1:8000", "Unexpected external request"
        self.trust_env = False
        return request(self, method, f"http://127.0.0.1:{port}" + parts.path +
                       ("?" + parts.query if parts.query else ""), *args, **kwargs)

    failed = False
    try:
        with patch.object(requests.Session, "request", local_only):
            for script in ("test_all_apis.py", "frontend_e2e_check.py", "wf_e2e.py", "wf_mutation_check.py"):
                sys.argv = [script]
                if script == "wf_mutation_check.py":
                    sys.argv.append(json.loads(Path("wf_e2e_result.json").read_text(encoding="utf-8"))["session_id"])
                try:
                    runpy.run_path(str(repo / script), run_name="__main__")
                except SystemExit as exc:
                    print(f"{script} exit {exc.code}")
                    failed |= bool(exc.code)
                except Exception as exc:
                    print(f"{script} exception {type(exc).__name__}: {exc}")
                    failed = True
        os.environ["AI_GRADE_PROBE_PORT"] = str(port)
        os.environ["AI_GRADE_PROBE_TOKEN"] = auth_middleware.create_access_token({"user_id": 1})
        probe = runpy.run_path(str(repo / "sse_stream_probe.py"))
        probe_result = probe["main"]()
        print("sse_stream_probe.py exit", probe_result)
        failed |= bool(probe_result)
        for _ in range(100):
            if not _shared_state._WORKFLOW_PROGRESS:
                break
            time.sleep(0.05)
        with database.SessionLocal() as db:
            from backend.models.user_models import WorkflowSession
            status = db.query(WorkflowSession).filter_by(project_name="SSE流式探测项目").one().status
            print("probe after disconnect:", status)
            failed |= status != "completed"
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        engine.dispose()

    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mysql-binary", type=Path, help="Use a fresh MySQL instance instead of SQLite")
    parser.add_argument("--work-dir", type=Path, help="Parent directory for disposable test artifacts")
    args = parser.parse_args()
    binary = args.mysql_binary.resolve(strict=True) if args.mysql_binary else None
    root = Path(tempfile.mkdtemp(prefix="ai-grade-replay-", dir=args.work_dir)).resolve()
    print(f"Isolated smoke artifacts: {root}")
    if binary:
        with isolated_mysql(binary, root) as database_url:
            return replay(root, database_url)
    return replay(root, f"sqlite:///{(root / 'test.sqlite').as_posix()}")


if __name__ == "__main__":
    raise SystemExit(main())
