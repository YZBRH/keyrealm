"""「不锁定」模式下的会话密钥持久化。

只有在设置 ``never_lock=1`` 且保险库处于解锁状态时才写入；手动锁定 / 关闭该设置 /
验证失败时立即删除。密钥按平台交给系统凭据保护：

- Windows：DPAPI（当前用户作用域，``CryptProtectData``）；
- macOS：系统钥匙串（``security`` 命令行）；
- Linux：优先 Secret Service（``secret-tool``），不可用时退化为
  「本机绑定加密文件」（machine-id + 用户 + 主机名 + 数据目录派生密钥）。

退化方案只能阻止把文件直接拷到别的机器使用，**不能**防御同一用户会话下的读取；
这是「不锁定」本身的安全代价，界面会明确提示。任何持久化失败都只记日志并返回
False，绝不能让解锁流程失败。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import database as db
from . import logger

_log = logger.get_logger(__name__)

FILENAME = "session.key"
_VERSION = 1
_KDF_SALT = b"keyrealm.session.v1"
_KDF_ROUNDS = 200_000
_KEY_BYTES = 32
_NONCE_BYTES = 12
_SERVICE = "com.keyrealm.app"
_ACCOUNT_PREFIX = "vault-session"

LABELS = {
    "dpapi": "Windows DPAPI",
    "keychain": "macOS 钥匙串",
    "secret-service": "Linux Secret Service",
    "machine": "本机绑定加密文件",
}


def _path() -> Path:
    return Path(db.DATA_DIR) / FILENAME


def _account() -> str:
    """按数据目录区分条目，支持同一用户配置多个保险库。"""
    digest = hashlib.sha256(str(_path().resolve()).encode("utf-8")).hexdigest()[:16]
    return f"{_ACCOUNT_PREFIX}:{digest}"


def _read_marker() -> Optional[dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_marker(protection: str, payload: str = "") -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    body = json.dumps(
        {"v": _VERSION, "protection": protection, "payload": payload},
        ensure_ascii=False,
    )
    tmp.write_text(body, encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def exists() -> bool:
    return _read_marker() is not None


def protection() -> str:
    marker = _read_marker() or {}
    value = str(marker.get("protection") or "")
    return value if value in LABELS else ""


def protection_label() -> str:
    return LABELS.get(protection(), "")


# ── Windows DPAPI ─────────────────────────────────────────

def _dpapi(data: bytes, protect: bool) -> bytes:
    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.c_ulong),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    crypt32 = ctypes.windll.crypt32
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    func.restype = ctypes.c_bool
    # CRYPTPROTECT_UI_FORBIDDEN = 1：后台进程不允许弹 UI。
    ok = func(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


# ── 系统凭据存储 ──────────────────────────────────────────

def _secret_tool() -> Optional[str]:
    return shutil.which("secret-tool")


def _secret_store(secret: str) -> bool:
    tool = _secret_tool()
    if not tool:
        return False
    proc = subprocess.run(
        [tool, "store", "--label", "Keyrealm 会话密钥",
         "service", "keyrealm", "account", _account()],
        input=secret.encode("utf-8"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=12,
        check=False,
    )
    if proc.returncode != 0:
        _log.info("Secret Service 写入失败：%s", proc.stderr.decode("utf-8", "replace").strip())
        return False
    return True


def _secret_load() -> Optional[str]:
    tool = _secret_tool()
    if not tool:
        return None
    proc = subprocess.run(
        [tool, "lookup", "service", "keyrealm", "account", _account()],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=12,
        check=False,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.decode("utf-8", "replace").strip()
    return value or None


def _secret_clear() -> None:
    tool = _secret_tool()
    if not tool:
        return
    subprocess.run(
        [tool, "clear", "service", "keyrealm", "account", _account()],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=12,
        check=False,
    )


def _keychain_store(secret: str) -> bool:
    proc = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", _SERVICE,
         "-a", _account(), "-w", secret],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        _log.info("钥匙串写入失败：%s", proc.stderr.decode("utf-8", "replace").strip())
        return False
    return True


def _keychain_load() -> Optional[str]:
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", _SERVICE, "-a", _account(), "-w"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.decode("utf-8", "replace").strip()
    return value or None


def _keychain_clear() -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", _SERVICE, "-a", _account()],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )


# ── 本机绑定加密（Linux / 兜底） ──────────────────────────

def _machine_id() -> str:
    if os.name == "nt":
        try:
            import winreg  # noqa: PLC0415

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
                return str(value)
        except OSError:
            return ""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


def _machine_key() -> bytes:
    user = str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "")
    parts = [
        _machine_id(),
        platform.node(),
        user,
        str(_path().resolve()),
    ]
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=_KDF_SALT,
        iterations=_KDF_ROUNDS,
    )
    return kdf.derive("|".join(parts).encode("utf-8"))


def _machine_seal(key: bytes) -> str:
    nonce = os.urandom(_NONCE_BYTES)
    blob = AESGCM(_machine_key()).encrypt(nonce, key, None)
    return base64.b64encode(nonce + blob).decode("ascii")


def _machine_open(payload: str) -> bytes:
    raw = base64.b64decode(payload)
    nonce, blob = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    return AESGCM(_machine_key()).decrypt(nonce, blob, None)


# ── 对外接口 ──────────────────────────────────────────────

def save(key: bytes, force: str = "") -> bool:
    """保存会话密钥。``force`` 仅供测试指定保护方式。"""
    if len(key) != _KEY_BYTES:
        return False
    mode = force or ("dpapi" if os.name == "nt" else
                     "keychain" if sys.platform == "darwin" else "secret-service")
    try:
        clear()
        if mode == "dpapi":
            payload = base64.b64encode(_dpapi(key, True)).decode("ascii")
            _write_marker("dpapi", payload)
            return True
        if mode == "keychain" and _keychain_store(base64.b64encode(key).decode("ascii")):
            _write_marker("keychain")
            return True
        if mode == "secret-service" and _secret_store(base64.b64encode(key).decode("ascii")):
            _write_marker("secret-service")
            return True
        # 系统凭据不可用 → 本机绑定加密文件
        _write_marker("machine", _machine_seal(key))
        return True
    except Exception as exc:  # noqa: BLE001 - 持久化失败不能影响解锁
        _log.warning("会话密钥保存失败：%s", exc)
        return False


def load() -> Optional[bytes]:
    marker = _read_marker()
    if not marker:
        return None
    protection = str(marker.get("protection") or "")
    payload = str(marker.get("payload") or "")
    try:
        if protection == "dpapi":
            raw = _dpapi(base64.b64decode(payload), False)
        elif protection == "machine":
            raw = _machine_open(payload)
        elif protection == "keychain":
            secret = _keychain_load()
            raw = base64.b64decode(secret) if secret else b""
        elif protection == "secret-service":
            secret = _secret_load()
            raw = base64.b64decode(secret) if secret else b""
        else:
            return None
        return raw if len(raw) == _KEY_BYTES else None
    except Exception as exc:  # noqa: BLE001 - 读取失败按没有持久化处理
        _log.warning("会话密钥读取失败（%s）：%s", protection or "unknown", exc)
        return None


def clear() -> None:
    """删除持久化密钥；系统凭据与标记文件都不留。"""
    try:
        if sys.platform == "darwin":
            _keychain_clear()
        elif os.name != "nt" and _secret_tool():
            _secret_clear()
    except Exception as exc:  # noqa: BLE001
        _log.debug("清理系统凭据失败：%s", exc)
    try:
        _path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log.warning("删除会话密钥文件失败：%s", exc)
