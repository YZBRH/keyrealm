"""
AES-256-GCM 加密模块
主密码 → PBKDF2 → AES 密钥 → 字段级加密
"""
import base64
import os
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ITERATIONS = 100_000
_SALT_BYTES = 16
_NONCE_BYTES = 12
_KEY_BYTES = 32
_VERIFY_PLAINTEXT = b"VAULT_OK"


def generate_salt() -> bytes:
    return os.urandom(_SALT_BYTES)


def derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=salt,
        iterations=_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def encrypt(key: bytes, plaintext: str) -> str:
    """加密字符串，返回 base64(nonce + ciphertext + tag)"""
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_BYTES)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(key: bytes, token: str) -> str:
    """解密 base64(nonce + ciphertext + tag)，失败抛出 ValueError"""
    raw = base64.b64decode(token)
    nonce = raw[:_NONCE_BYTES]
    ct = raw[_NONCE_BYTES:]
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ct, None)
    except Exception as e:
        raise ValueError("解密失败：密钥错误或数据已损坏") from e
    return plaintext.decode("utf-8")


def create_verify_token(key: bytes) -> str:
    """创建主密码验证密文，存入 settings 表"""
    return encrypt(key, _VERIFY_PLAINTEXT.decode())


def check_verify_token(key: bytes, token: str) -> bool:
    """验证主密码是否正确"""
    try:
        return decrypt(key, token) == _VERIFY_PLAINTEXT.decode()
    except ValueError:
        return False


def encrypt_dict(key: bytes, data: dict) -> str:
    """加密整个字典（JSON 序列化后加密）"""
    import json
    return encrypt(key, json.dumps(data, ensure_ascii=False))


def decrypt_dict(key: bytes, token: str) -> dict:
    """解密字典"""
    import json
    return json.loads(decrypt(key, token))


def generate_token() -> str:
    """生成 MCP 访问令牌：kr_ + 32位hex"""
    return "kr_" + os.urandom(16).hex()


def hash_token(token: str) -> str:
    """SHA-256 哈希 Token（用于数据库存储）"""
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_secret(value: str) -> str:
    """敏感值掩码展示：保留首 6 位与末 4 位（如 sk-123…abcd）。"""
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "…"
    return f"{value[:6]}…{value[-4:]}"


def mask_display(value: str) -> str:
    """界面展示用掩码：长度 <= 10 只显示前 4 位；更长的与 ``mask_secret`` 一致。

    与 ``mask_secret`` 分开，避免为了界面预览把日志里的短凭据也暴露得更多。
    """
    if not value:
        return ""
    if len(value) <= 10:
        return value[:4] + "…"
    return mask_secret(value)
