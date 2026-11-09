"""批次 24（C6 密钥存储）的变异测试。

锁的是：写进去是不是密文、老明文还能不能读、没钥匙会不会静默降级、
对外展示有没有把密钥片段漏出来。每个变异体先离线 compile()。
"""
import subprocess
import sys
from pathlib import Path

PY = ".venv/Scripts/python"
TARGET_TESTS = "tests/test_secret_storage.py"
BOX = "backend/services/secret_box.py"
ADMIN = "backend/routes/admin_routes.py"

MUTATIONS = [
    ("掩码退回「前 8 位 + ***」（旧实现，泄露半截密钥）",
     BOX,
     """    return SECRET_MASK if value else None""",
     """    return (value[:8] + "***") if value else None"""),
    ("掩码随值变化（泄露长度信息）",
     BOX,
     """    return SECRET_MASK if value else None""",
     """    return ("*" * len(value)) if value else None"""),
    ("写库不加密（直接存明文）",
     BOX,
     """    return SECRET_PREFIX + token""",
     """    return text"""),
    ("去掉幂等判断（密文被二次加密，读不回来）",
     BOX,
     """    if is_encrypted(text):
        return text""",
     """    if False:
        return text"""),
    ("空值存成空串密文（has_api_key 永真）",
     BOX,
     """    if not text:
        return None""",
     """    if not text:
        return "" """),
    ("历史明文不再兼容（老数据全废）",
     BOX,
     """    if not is_encrypted(text):
        return text  # 历史明文，平滑兼容""",
     """    if not is_encrypted(text):
        return \"\""""),
    ("没配钥匙时静默降级（不打告警）",
     BOX,
     """        if not _warned_plaintext:
            _warned_plaintext = True
            logger.warning(""",
     """        if False:
            _warned_plaintext = True
            logger.warning("""),
    ("没配钥匙时直接报错（跑着的系统起不来）",
     BOX,
     """    fernet = _fernet()
    if fernet is None:
        if not _warned_plaintext:""",
     """    fernet = _fernet()
    if fernet is None:
        raise RuntimeError("SECRET_ENCRYPTION_KEY 未配置")
        if not _warned_plaintext:"""),
    ("解不开时抛异常（升级成 500）",
     BOX,
     """    fernet = _fernet()
    if fernet is None:
        logger.error("密文存在但 SECRET_ENCRYPTION_KEY 未配置，无法解密")
        return \"\"""",
     """    fernet = _fernet()
    if fernet is None:
        raise RuntimeError("密文存在但 SECRET_ENCRYPTION_KEY 未配置")"""),
    ("坏密文不兜底（InvalidToken 冒泡）",
     BOX,
     """    except InvalidToken:
        logger.error("密钥解密失败：SECRET_ENCRYPTION_KEY 可能已更换")
        return \"\"""",
     """    except InvalidToken:
        raise"""),
    ("任意口令不做派生（换格式就崩）",
     BOX,
     """    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)""",
     """    return raw.encode()"""),
    ("is_configured 永远为真（状态骗人）",
     BOX,
     """    return bool(getattr(settings, "SECRET_ENCRYPTION_KEY", "") or "")""",
     """    return True"""),
    ("新建模型时明文落库",
     ADMIN,
     """        api_key=encrypt_secret(req.api_key), api_base_url=req.api_base_url, is_enabled=req.is_enabled,""",
     """        api_key=req.api_key, api_base_url=req.api_base_url, is_enabled=req.is_enabled,"""),
    ("更新模型时明文落库",
     ADMIN,
     """        if req.api_key != SECRET_MASK:
            model.api_key = encrypt_secret(req.api_key)""",
     """        model.api_key = req.api_key"""),
    ("更新时不识别掩码回传（真密钥被 ******** 冲掉）",
     ADMIN,
     """        if req.api_key != SECRET_MASK:
            model.api_key = encrypt_secret(req.api_key)""",
     """        model.api_key = encrypt_secret(req.api_key)"""),
    ("列表接口直接返回库里原值",
     ADMIN,
     """                "model_id": m.model_id, "api_key": mask_secret(m.api_key),
                "has_api_key": bool(m.api_key),""",
     """                "model_id": m.model_id, "api_key": m.api_key,
                "has_api_key": bool(m.api_key),"""),
    ("列表接口不报 has_api_key",
     ADMIN,
     """                "model_id": m.model_id, "api_key": mask_secret(m.api_key),
                "has_api_key": bool(m.api_key),""",
     """                "model_id": m.model_id, "api_key": mask_secret(m.api_key),"""),
    ("用户详情重新回显 password_hash",
     ADMIN,
     """            "role": user.role.value if isinstance(user.role, UserRole) else user.role,
            "is_active": user.is_active,
            "must_change_password": bool(getattr(user, "must_change_password", False)),""",
     """            "password_hash": user.password_hash,
            "role": user.role.value if isinstance(user.role, UserRole) else user.role,
            "is_active": user.is_active,
            "must_change_password": bool(getattr(user, "must_change_password", False)),"""),
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
