"""批次 24（C6 首登强制改密）的变异测试。

锁的是：初始口令到底拦不拦、拦截白名单会不会误伤或放宽、
改完密能不能解锁、老库升级会不会误伤改过密码的人、
前端会不会把「要改密」误当成「登录失效」把人踢出去。

前端两个变异体要用 node 跑 tests/test_shared_frontend.cjs —— 那里的用例
是真的加载 api.js、喂一个 403 响应、看它到底清不清 token。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python"
TARGET_TESTS = "tests/test_force_password_change.py"
FRONTEND_TESTS = "tests/test_shared_frontend.cjs"
MW = "backend/middleware/auth_middleware.py"
AUTH = "backend/services/auth_service.py"
AUTH_R = "backend/routes/auth_routes.py"
CLIENT_R = "backend/routes/client_routes.py"
ADMIN_R = "backend/routes/admin_routes.py"
MODEL = "backend/models/user_models.py"
PATCH = "backend/utils/schema_patches.py"
APIJS = "frontend/shared/api.js"

MUTATIONS = [
    ("初始口令不再拦截（整段护栏被删）",
     MW,
     """    if getattr(user, "must_change_password", False) and not is_password_change_exempt(request.url.path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="首次登录请先修改初始密码",
            headers={"X-Password-Change-Required": "1"},
        )""",
     """    pass"""),
    ("白名单变成全放行（等于没拦）",
     MW,
     """    if getattr(user, "must_change_password", False) and not is_password_change_exempt(request.url.path):""",
     """    if getattr(user, "must_change_password", False) and False:"""),
    ("白名单前缀匹配过宽（/api/client/passwordreset 被顺带放过）",
     MW,
     """    return any(clean == p or clean.startswith(p + "/") for p in PASSWORD_CHANGE_EXEMPT_PATHS)""",
     """    return any(clean.startswith(p) for p in PASSWORD_CHANGE_EXEMPT_PATHS)"""),
    ("403 不带标记头（前端会当成登录失效踢人）",
     MW,
     """            detail="首次登录请先修改初始密码",
            headers={"X-Password-Change-Required": "1"},""",
     """            detail="首次登录请先修改初始密码","""),
    ("先判改密再判禁用（禁用账号被放行到改密接口）",
     MW,
     """    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用",
        )""",
     """    if False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用",
        )"""),
    ("改密后不清标志（改完还是被自己的 403 拦着）",
     CLIENT_R,
     """    current_user.must_change_password = False""",
     """    current_user.must_change_password = True"""),
    ("默认管理员不置位（初始口令可长期使用）",
     AUTH,
     """                role="admin",
                must_change_password=True,""",
     """                role="admin",
                must_change_password=False,"""),
    ("默认管理员口令打进日志",
     AUTH,
     """            logger.info("默认管理员账号已创建（初始口令见文档，首次登录必须修改）")""",
     """            logger.info("默认管理员账号已创建: admin/Admin@123456")"""),
    ("替代分支仍把元组塞进 String 列",
     AUTH,
     """                password_hash=f"{raw_salt}:{raw_hash}",""",
     """                password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),"""),
    ("迁移时一刀切（把改过密码的人也打标）",
     AUTH,
     """        if any(verify_password(pwd, stored_hash, salt) for pwd in KNOWN_DEFAULT_PASSWORDS):""",
     """        if True:"""),
    ("迁移时跳过已置位（重复运行会清空状态）",
     AUTH,
     """        if getattr(user, "must_change_password", False):
            continue""",
     """        if False:
            continue"""),
    ("管理员重置密码后不置位",
     ADMIN_R,
     """        user.must_change_password = True
        # 管理员重置口令 → 该用户已登录的会话必须立刻失效（批次 25）
        user.token_version = int(getattr(user, "token_version", 0) or 0) + 1
        changed.append("password")""",
     """        user.token_version = int(getattr(user, "token_version", 0) or 0) + 1
        changed.append("password")"""),
    ("管理员建号后不置位",
     AUTH_R,
     """        user = create_user(db, req.username, req.password, req.phone, must_change_password=True)""",
     """        user = create_user(db, req.username, req.password, req.phone)"""),
    ("自助注册也被强制改密（误伤本人设的口令）",
     AUTH_R,
     """        user = create_user(db, req.username, req.password, req.phone)""",
     """        user = create_user(db, req.username, req.password, req.phone, must_change_password=True)"""),
    ("登录响应不带改密标志（前端无法直达改密面板）",
     AUTH_R,
     """                "must_change_password": bool(getattr(user, "must_change_password", False)),""",
     """                "must_change_password": False,"""),
    ("模型列允许 NULL（老行无法判定）",
     MODEL,
     """    must_change_password = Column(Boolean, default=False, nullable=False, server_default="0")""",
     """    must_change_password = Column(Boolean, default=False, nullable=True)"""),
    ("模型列没有 server_default（ALTER 加列时老行是 NULL）",
     MODEL,
     """    must_change_password = Column(Boolean, default=False, nullable=False, server_default="0")""",
     """    must_change_password = Column(Boolean, default=False, nullable=False)"""),
    ("补丁不检查列是否存在（每次启动都 ALTER）",
     PATCH,
     """    if _column_exists(conn, table, column):
        return False""",
     """    if False:
        return False"""),
    ("前端不识别强制改密响应头",
     APIJS,
     """                if (response.status === 403 && isPasswordChangeRequired(response)) {""",
     """                if (false) {"""),
    ("被迫改密时清掉 token（用户再也调不了改密接口）",
     APIJS,
     """        var target = (CFG.routes && CFG.routes.client) || '/client/';""",
     """        clearAuth();
        var target = (CFG.routes && CFG.routes.client) || '/client/';"""),
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
        if path.endswith(".py") and not compile_ok(path):
            Path(path).write_text(originals[path], encoding="utf-8")
            continue
        if path == APIJS:
            cmd = ["node", "--test", FRONTEND_TESTS]
        else:
            cmd = [PY, "-m", "pytest", *TARGET_TESTS.split(), "-q", "--no-header",
                   "-p", "no:cacheprovider"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if path == APIJS:
            line = [ln for ln in result.stdout.splitlines() if ln.startswith("# fail")]
            tail = line[-1] if line else ""
        else:
            line = [item for item in result.stdout.splitlines()
                    if "passed" in item or "failed" in item]
            tail = line[-1] if line else ""
        caught = result.returncode != 0
        print(f"{'抓住' if caught else '漏掉'}  {name}   {tail}")
        if not caught:
            failures.append(name)
        Path(path).write_text(originals[path], encoding="utf-8")
finally:
    for path, text in originals.items():
        Path(path).write_text(text, encoding="utf-8")

print("\n还原完成，漏掉的变异：", failures or "无")
sys.exit(1 if failures else 0)
