"""数据库密钥加密 + 掩码不可逆（批次 24 = C6 安全收尾）

改前的样子：

  * `model_configs.api_key` 明文落库（`admin_create_model` 直接 `api_key=req.api_key`）；
  * `admin_list_models` 返回 `m.api_key[:8] + "***"`——**把密钥前 8 位交给前端**，
    而多数厂商 Key 的前缀是固定的（`sk-`、`sk-proj-` 之类），前 8 位几乎没有遮蔽作用；
  * `admin_get_user` 把 `password_hash` 一起返回，哈希是可离线爆破的凭据材料。

口径：
  * 写入即密文，带 `enc:v1:` 版本前缀；
  * **历史明文原样可读**（写时加密、读时兼容），不需要停机迁移；
  * 没配密钥就降级明文，但必须告警——安全加固不该让跑着的系统起不来，
    可也不能让人以为它生效了；
  * 对外展示只给固定掩码，不含原文任何片段。
"""
from types import SimpleNamespace

import pytest

from backend.routes import admin_routes
from backend.services import secret_box


# ═══════════════════════════════════════════════════════
#  夹具：把密钥配置与「已告警」标志隔离在单个用例内
# ═══════════════════════════════════════════════════════

@pytest.fixture
def with_key(monkeypatch):
    """配置了密钥的环境。"""
    key = secret_box.generate_key()
    monkeypatch.setattr(secret_box, "settings", SimpleNamespace(SECRET_ENCRYPTION_KEY=key))
    monkeypatch.setattr(secret_box, "_warned_plaintext", True)
    return key


@pytest.fixture
def without_key(monkeypatch):
    """没配密钥的环境（生产上忘记配就是这个状态）。"""
    monkeypatch.setattr(secret_box, "settings", SimpleNamespace(SECRET_ENCRYPTION_KEY=""))
    monkeypatch.setattr(secret_box, "_warned_plaintext", False)


# ═══════════════════════════════════════════════════════
#  一、加解密往返
# ═══════════════════════════════════════════════════════

def test_encrypt_adds_versioned_prefix(with_key):
    token = secret_box.encrypt_secret("sk-abcdef1234567890")
    assert token.startswith(secret_box.SECRET_PREFIX), "密文必须带版本前缀，否则无法识别历史明文"
    assert "sk-abcdef1234567890" not in token, "密文里不该出现明文"


def test_roundtrip(with_key):
    plain = "sk-live-0123456789abcdef"
    assert secret_box.decrypt_secret(secret_box.encrypt_secret(plain)) == plain


def test_encrypt_is_idempotent(with_key):
    """重复保存（前端把密文回传、或 update 走两遍）不该二次加密。"""
    once = secret_box.encrypt_secret("sk-xyz")
    twice = secret_box.encrypt_secret(once)
    assert once == twice
    assert secret_box.decrypt_secret(twice) == "sk-xyz"


def test_two_encryptions_of_same_plaintext_differ(with_key):
    """Fernet 自带随机 IV：同一明文两次加密结果不同，避免「相同密钥可被肉眼比对」。"""
    a = secret_box.encrypt_secret("sk-same")
    b = secret_box.encrypt_secret("sk-same")
    assert a != b
    assert secret_box.decrypt_secret(a) == secret_box.decrypt_secret(b) == "sk-same"


def test_empty_becomes_none(with_key):
    """空值存 None：不要留下一个「加密后的空串」，否则 has_api_key 永远为真。"""
    assert secret_box.encrypt_secret(None) is None
    assert secret_box.encrypt_secret("") is None


# ═══════════════════════════════════════════════════════
#  二、历史明文平滑迁移
# ═══════════════════════════════════════════════════════

def test_legacy_plaintext_reads_through(with_key):
    """改版前落库的明文，读的时候必须原样返回——否则老数据全废。"""
    assert secret_box.decrypt_secret("sk-legacy-plain-key") == "sk-legacy-plain-key"


def test_legacy_plaintext_is_recognized_as_not_encrypted(with_key):
    assert secret_box.is_encrypted("sk-legacy-plain-key") is False
    assert secret_box.is_encrypted(secret_box.encrypt_secret("sk-x")) is True


def test_legacy_plaintext_gets_encrypted_on_next_write(with_key):
    """平滑迁移的机制：不主动扫库，下一次写入时自然变成密文。"""
    legacy = "sk-plain-from-2023"
    assert secret_box.is_encrypted(legacy) is False
    assert secret_box.is_encrypted(secret_box.encrypt_secret(legacy)) is True


def test_decrypt_empty_returns_empty_string(with_key):
    assert secret_box.decrypt_secret(None) == ""
    assert secret_box.decrypt_secret("") == ""


# ═══════════════════════════════════════════════════════
#  三、没配密钥：降级但不静默
# ═══════════════════════════════════════════════════════

def test_no_key_falls_back_to_plaintext(without_key):
    assert secret_box.is_configured() is False
    assert secret_box.encrypt_secret("sk-plain") == "sk-plain"


def test_no_key_cannot_decrypt_ciphertext(without_key):
    """密文在、钥匙丢了：返回空串而不是抛异常。

    抛异常会把「密钥配错了」升级成「整个配置页 500」；返回空串还能落到
    「未配置 API Key」的既有提示分支。
    """
    assert secret_box.decrypt_secret(secret_box.SECRET_PREFIX + "garbage") == ""


def test_no_key_warns_only_once(without_key, monkeypatch):
    calls = []
    monkeypatch.setattr(secret_box.logger, "warning", lambda msg, *a, **k: calls.append(msg))
    secret_box.encrypt_secret("a")
    secret_box.encrypt_secret("b")
    secret_box.encrypt_secret("c")
    assert len(calls) == 1, f"降级告警应只打一次，实际 {len(calls)} 次（会刷屏）"


def test_describe_storage_reflects_config(monkeypatch):
    monkeypatch.setattr(secret_box, "settings", SimpleNamespace(SECRET_ENCRYPTION_KEY="k"))
    assert "已启用" in secret_box.describe_storage()
    monkeypatch.setattr(secret_box, "settings", SimpleNamespace(SECRET_ENCRYPTION_KEY=""))
    assert "未启用" in secret_box.describe_storage()


# ═══════════════════════════════════════════════════════
#  四、换钥匙 / 坏密文
# ═══════════════════════════════════════════════════════

def test_wrong_key_returns_empty(monkeypatch):
    monkeypatch.setattr(secret_box, "_warned_plaintext", True)
    monkeypatch.setattr(secret_box, "settings",
                        SimpleNamespace(SECRET_ENCRYPTION_KEY=secret_box.generate_key()))
    token = secret_box.encrypt_secret("sk-secret")

    monkeypatch.setattr(secret_box, "settings",
                        SimpleNamespace(SECRET_ENCRYPTION_KEY=secret_box.generate_key()))
    assert secret_box.decrypt_secret(token) == ""


def test_garbage_ciphertext_returns_empty(with_key):
    assert secret_box.decrypt_secret(secret_box.SECRET_PREFIX + "not-a-token") == ""


def test_arbitrary_passphrase_works_and_is_stable(monkeypatch):
    """允许 .env 里填随便一串口令（不必是 Fernet 的 44 字符 key）。"""
    monkeypatch.setattr(secret_box, "_warned_plaintext", True)
    monkeypatch.setattr(secret_box, "settings", SimpleNamespace(SECRET_ENCRYPTION_KEY="我的口令-123"))
    token = secret_box.encrypt_secret("sk-a")
    # 必须真的加密：只断言“往返相等”会被“悄悄降级成明文”蒙混过去（明文往返也相等）
    assert secret_box.is_encrypted(token), "任意口令应能真正加密，而不是静默降级成明文"
    assert secret_box.decrypt_secret(token) == "sk-a"

    # 同一口令重新实例化（模拟重启）仍能解开
    monkeypatch.setattr(secret_box, "settings", SimpleNamespace(SECRET_ENCRYPTION_KEY="我的口令-123"))
    assert secret_box.decrypt_secret(token) == "sk-a"


def test_generate_key_is_valid_fernet_key():
    from cryptography.fernet import Fernet
    key = secret_box.generate_key()
    Fernet(key.encode())  # 不抛即合法


# ═══════════════════════════════════════════════════════
#  五、掩码：不得泄露任何原文片段（核心回归）
# ═══════════════════════════════════════════════════════

def test_mask_hides_every_character(with_key):
    """旧实现返回 `value[:8] + "***"`，前 8 位是真密钥的一部分。"""
    plain = "sk-proj-abcdefghijklmnop"
    masked = secret_box.mask_secret(plain)
    assert masked == secret_box.SECRET_MASK
    assert plain[:8] not in masked, f"掩码泄露了密钥前缀: {masked}"
    assert not any(ch in masked for ch in plain if ch.isalnum())


def test_mask_is_same_for_every_value(with_key):
    """掩码不携带长度信息（长度本身也是线索）。"""
    assert secret_box.mask_secret("a") == secret_box.mask_secret("a" * 200)


def test_mask_none_for_missing(with_key):
    assert secret_box.mask_secret(None) is None
    assert secret_box.mask_secret("") is None


# ═══════════════════════════════════════════════════════
#  六、路由接入：写加密、读掩码
# ═══════════════════════════════════════════════════════

class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def first(self):
        return self._result if not isinstance(self._result, list) else (
            self._result[0] if self._result else None)

    def all(self):
        return self._result if isinstance(self._result, list) else [self._result]

    def scalar(self):
        return 0


class _FakeDB:
    def __init__(self, result=None):
        self._result = result
        self.added = []
        self.commits = 0

    def query(self, *a, **k):
        return _FakeQuery(self._result)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        pass


def _make_model(api_key, name="m1"):
    from datetime import datetime
    from backend.models.user_models import ModelConfig
    m = ModelConfig(platform="deepseek", model_name=name, model_id=name,
                    api_key=api_key, is_enabled=True, sort_order=1,
                    created_at=datetime(2026, 1, 1))
    return m


def test_list_models_returns_mask_not_prefix(with_key):
    db = _FakeDB([_make_model(secret_box.encrypt_secret("sk-proj-REALKEY123456"))])
    resp = admin_routes.admin_list_models(db)
    row = resp["data"][0]

    assert row["api_key"] == secret_box.SECRET_MASK
    assert row["has_api_key"] is True
    assert "sk-proj-R" not in str(resp), "列表响应里仍出现密钥片段"
    assert "REALKEY" not in str(resp)


def test_list_models_reports_missing_key(with_key):
    db = _FakeDB([_make_model(None)])
    row = admin_routes.admin_list_models(db)["data"][0]
    assert row["api_key"] is None
    assert row["has_api_key"] is False


def test_create_model_stores_ciphertext(with_key):
    from backend.routes.admin_routes import ModelConfigRequest
    db = _FakeDB(None)
    admin_routes.admin_create_model(
        ModelConfigRequest(platform="deepseek", model_name="m1", api_key="sk-brand-new"), db)

    stored = db.added[0].api_key
    assert secret_box.is_encrypted(stored), "新建模型时密钥必须加密落库"
    assert secret_box.decrypt_secret(stored) == "sk-brand-new"


def test_update_model_stores_ciphertext(with_key):
    from backend.routes.admin_routes import ModelConfigRequest
    model = _make_model(secret_box.encrypt_secret("sk-old"))
    db = _FakeDB(model)
    admin_routes.admin_update_model(
        1, ModelConfigRequest(platform="deepseek", model_name="m1", api_key="sk-new"), db)

    assert secret_box.is_encrypted(model.api_key)
    assert secret_box.decrypt_secret(model.api_key) == "sk-new"


def test_update_model_ignores_mask_echo(with_key):
    """前端把列表里的 `********` 原样回传时，不能拿它覆盖真正的密文。"""
    from backend.routes.admin_routes import ModelConfigRequest
    original = secret_box.encrypt_secret("sk-keep-me")
    model = _make_model(original)
    db = _FakeDB(model)

    admin_routes.admin_update_model(
        1, ModelConfigRequest(platform="deepseek", model_name="m1",
                              api_key=secret_box.SECRET_MASK), db)

    assert model.api_key == original, "掩码回传把真密钥冲掉了"
    assert secret_box.decrypt_secret(model.api_key) == "sk-keep-me"


def test_update_model_keeps_key_when_field_empty(with_key):
    from backend.routes.admin_routes import ModelConfigRequest
    original = secret_box.encrypt_secret("sk-untouched")
    model = _make_model(original)
    admin_routes.admin_update_model(
        1, ModelConfigRequest(platform="deepseek", model_name="m1", api_key=None), _FakeDB(model))
    assert model.api_key == original


# ═══════════════════════════════════════════════════════
#  七、admin_get_user 不再回显 password_hash
# ═══════════════════════════════════════════════════════

def test_get_user_does_not_expose_password_hash(with_key):
    from datetime import datetime
    from backend.models.user_models import User, UserRole

    user = User(id=7, username="u1", password_hash="abc:def", role=UserRole.USER,
                is_active=True, tokens_used=0, must_change_password=False,
                created_at=datetime(2026, 1, 1))
    db = SimpleNamespace(
        query=lambda *a, **k: _FakeQuery(0),  # project_count 的 func.count 走 scalar
    )
    # 让两次查询分别返回不同结果：先 user，再 0 / [] 列表
    seq = [_FakeQuery(user), _FakeQuery(0), _FakeQuery([])]

    class _SeqDB:
        def query(self, *a, **k):
            return seq.pop(0) if seq else _FakeQuery(0)

    resp = admin_routes.admin_get_user(7, _SeqDB())
    assert "password_hash" not in resp["data"], "用户详情仍在回显口令哈希"
    assert "must_change_password" in resp["data"]
