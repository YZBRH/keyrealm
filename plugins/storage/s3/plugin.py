"""通用「S3 兼容对象存储」验证插件（plugins/storage/ 类别下的独立子插件）。

面向**任意 S3 兼容服务**：MinIO、Ceph RGW、SeaweedFS，以及各云厂商的 S3 兼容端点
（AWS S3、阿里云 OSS 的 S3 端点等）。签名走类别共享的 SigV4（``storage_s3v4``），
XML 解析与带进度的流式下载走 ``storage_common``。

设计要点
--------
- **端点由用户填**（可带端口，如 MinIO 的 ``http://minio.example.com:9000``），
  寻址方式可选**路径式**（默认，MinIO / IP 端点用）或**虚拟主机式**（AWS S3 等）。
- **自签名证书**的自建服务可勾「跳过 TLS 证书校验」（仅对该凭据生效）。
- 全部调用均为只读，不产生任何远端变更。

本插件提供的操作（``actions``）：

- ``list_buckets``     桶列表（S3 ListBuckets；双击「预览」下钻到对象）
- ``list_objects``     对象浏览（ListObjectsV2 + delimiter，文件与文件夹分行）
- ``download_object``  下载单个对象（``save_to``，前端弹「另存为」）
- ``download_prefix``  把某个前缀（文件夹）下的对象并发拉取后打包成 zip 下载

与 ``cloud/aws`` 等插件的差异：这里支持 **path 风格 + 端口**（MinIO / 自建）、**任意端点**，
故不并入云厂商插件。

参考文档：https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-authenticating-requests.html
参考文档：https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlsplit

from backend.plugin_sdk import VerifyResult, log, make_client, mask_secret

import storage_s3v4 as s3v4
from storage_common import (
    Cancelled,
    child_text,
    download_with_progress,
    human_size,
    redact,
    remove_quietly,
    truthy,
    xml_error,
    xml_findall,
    xml_root,
    xml_text,
)

# ── 常量 ──────────────────────────────────────────────────

DEFAULT_REGION = "us-east-1"
_SERVICE = "s3"
# 桶名：允许字母数字与 . _ -（宽松兼容各类 S3 实现），但绝不允许斜杠/空白/.. （防路径注入）
_BUCKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,62}$")

_PAGE_SIZE = 50
_ENUM_PAGE = 1000

# 打包下载上限：**宁可明确报错，也不静默截断**（前端会原样展示 error/output）
MAX_ZIP_OBJECTS = 1000
MAX_ZIP_BYTES = 512 * 1024 * 1024
_ZIP_WORKERS = 8

# 明确代表「凭据被拒」/「签名通过但无权限」的错误码
_INVALID_CODES = {
    "InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidToken",
    "ExpiredToken", "TokenRefreshRequired",
}
_DENIED_CODES = {"AccessDenied", "AllAccessDisabled", "AccountProblem"}

REQUIRED = [
    ("endpoint", "服务端点"),
    ("access_key", "Access Key"),
    ("secret_key", "Secret Key"),
]


# ── 小工具 ────────────────────────────────────────────────

def _field(data: dict, key: str) -> str:
    return str((data or {}).get(key) or "").strip()


def _short(text: Any, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


# ── 端点解析 / 目标拼装 ────────────────────────────────────

def _endpoint_info(data: dict) -> tuple[dict | None, str]:
    """把 ``endpoint`` 规范化成 ``{scheme, host, port}``；非法时返回 ``(None, 原因)``。"""
    raw = _field(data, "endpoint")
    if not raw:
        return None, "服务端点为空"
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except Exception:  # noqa: BLE001
        return None, "服务端点格式不合法"
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None, "服务端点只支持 http / https"
    host = parts.hostname or ""
    if not host or parts.path not in ("", "/") or parts.query or parts.fragment:
        return None, "服务端点只需主机（可带端口），不要带桶名、路径或查询串"
    try:
        port = parts.port
    except ValueError:
        return None, "服务端口不合法"
    return {"scheme": scheme, "host": host, "port": port}, ""


def _host_port(ep: dict) -> str:
    return f"{ep['host']}:{ep['port']}" if ep.get("port") else ep["host"]


def _target(ep: dict, data: dict, bucket: str, key: str = "") -> tuple[str, str]:
    """返回 ``(host 头, path)``。**虚拟主机式**把桶放进 host，**路径式**放进 path。"""
    addressing = (_field(data, "addressing") or "path").lower()
    key_path = "/" + key.lstrip("/") if key else "/"
    if addressing == "virtual" and bucket:
        return f"{bucket}.{ep['host']}" + (f":{ep['port']}" if ep.get("port") else ""), key_path
    if bucket:
        return _host_port(ep), f"/{bucket}{key_path}"
    return _host_port(ep), key_path


def _bucket_name(params: dict) -> tuple[str, str]:
    bucket = _field(params, "bucket")
    if not bucket:
        return "", "缺少桶名"
    if not _BUCKET_RE.match(bucket) or ".." in bucket:
        return "", "桶名不合法"
    return bucket, ""


def _region(data: dict, params: dict | None = None) -> str:
    return ((_field(params or {}, "region") or _field(data, "region")) or DEFAULT_REGION)


# ── HTTP 客户端（可选跳过 TLS 校验）────────────────────────
# 代理 / 超时 / TLS 版本由框架的 make_client() 统一施加，这里只决定是否关证书校验。

def _client(data: dict):
    if truthy(data.get("skip_tls_verify")):
        log("已对该凭据关闭 TLS 证书校验（skip_tls_verify=true）")
        return make_client(verify=False)
    return make_client()


def _signed_url(data: dict, ep: dict, bucket: str, key: str, query: dict | None,
                region: str, method: str = "GET", payload: bytes = b"") -> tuple[str, dict]:
    """按当前凭据签名，返回 ``(url, headers)``（url 的 host 与签名用的 host 一致）。"""
    host, path = _target(ep, data, bucket, key)
    q = s3v4.canonical_query(query or {})
    headers = s3v4.sign(
        method=method, host=host, path=path, query=q, payload=payload,
        access_key=_field(data, "access_key"), secret_key=_field(data, "secret_key"),
        session_token=_field(data, "session_token"), region=region, service=_SERVICE,
    )
    url = f"{ep['scheme']}://{host}{path}" + (f"?{q}" if q else "")
    return url, headers


def _failure(resp, data: dict) -> str:
    """把失败响应转成一句人话：优先错误码/消息；区域写错时给出**正确区域**提示。"""
    code, message = xml_error(resp.text or "")
    detail = message or code or f"HTTP {resp.status_code}"
    hint = ""
    root = xml_root(resp.text or "")
    if code in _INVALID_CODES or resp.status_code == 400:
        region = xml_text(root, "Region")
        if region and region != _region(data):
            hint = f"（服务端要求区域 {region}，请把凭据里的「区域」改成 {region}）"
    return redact(f"请求失败：{detail}{hint}", _field(data, "access_key"), _field(data, "secret_key"))


# ── 验证 ──────────────────────────────────────────────────

def validate(data) -> list[str]:
    """服务端兜底校验（``required`` 只在前端拦）。"""
    data = data or {}
    errors = []
    for key, label in REQUIRED:
        if not _field(data, key):
            errors.append(f"缺少必填字段：{label}")
    if _field(data, "endpoint"):
        _ep, error = _endpoint_info(data)
        if error:
            errors.append(error)
    return errors


def verify(data) -> VerifyResult:
    data = data or {}
    errors = validate(data)
    if errors:
        return VerifyResult(status="invalid", message="；".join(errors))
    return _verify(data)


def _verify(data: dict) -> VerifyResult:
    ep, error = _endpoint_info(data)
    if error:
        return VerifyResult(status="error", message=error)
    region = _region(data)
    ak = _field(data, "access_key")
    log(f"校验 S3 兼容存储 {ep['scheme']}://{_host_port(ep)}（AK {mask_secret(ak)}）")
    try:
        with _client(data) as client:
            url, headers = _signed_url(data, ep, "", "", None, region)
            resp = client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 - 边界兜底
        return VerifyResult(status="error", message=f"连接失败：{type(exc).__name__}")

    if resp.status_code == 200:
        count = len(xml_findall(xml_root(resp.text), "Bucket"))
        return VerifyResult(status="valid", message=f"连接成功（ListBuckets，{count} 个桶）",
                            account_info={"bucket_count": count})
    code, _message = xml_error(resp.text or "")
    if code in _INVALID_CODES:
        want = xml_text(xml_root(resp.text or ""), "Region")
        extra = (f"（服务端要求区域 {want}，请把凭据里的「区域」改成 {want}）"
                 if want and want != _region(data) else "")
        return VerifyResult(status="invalid", message=f"凭据无效（{code}）{extra}")
    # 签名通过但无列举桶权限 —— 凭据本身有效
    if code in _DENIED_CODES or resp.status_code in (401, 403):
        return VerifyResult(status="valid",
                            message=f"凭据有效，但无列举桶权限（{code or resp.status_code}）")
    return VerifyResult(status="error",
                        message=redact(f"HTTP {resp.status_code}：{_short(resp.text)}", ak))


# ── 操作声明 ──────────────────────────────────────────────

_BUCKET_COLUMNS = [
    {"key": "name", "label": "桶名", "mono": True},
    {"key": "creationDate", "label": "创建时间", "width": 200},
    {"key": "region", "label": "区域", "width": 140},
]

_OBJECT_COLUMNS = [
    {"key": "key", "label": "对象 Key", "mono": True},
    {"key": "type", "label": "类型", "width": 80, "type": "tag"},
    {"key": "size", "label": "大小", "width": 100, "type": "number"},
    {"key": "lastModified", "label": "最后修改", "width": 200},
]

_OBJECT_ROW_ACTION = {
    "id": "list_objects", "label": "预览",
    "params": {"bucket": "$row.name", "region": "$row.region"},
}

# 文件夹行有 folderPath（「进入」「打包下载」可用），文件行有 fileKey（「下载」可用）——
# 对方没有的键由前端自动置灰，绝不会传 undefined 过来。
_OBJECT_ROW_ACTIONS = [
    {"id": "list_objects", "label": "进入",
     "params": {"bucket": "$row.bucket", "prefix": "$row.folderPath", "region": "$row.region"}},
    {"id": "download_object", "label": "下载",
     "params": {"bucket": "$row.bucket", "key": "$row.fileKey", "region": "$row.region"}},
    {"id": "download_prefix", "label": "打包下载",
     "params": {"bucket": "$row.bucket", "prefix": "$row.folderPath", "region": "$row.region"}},
]

_BUCKET_FIELD = {"key": "bucket", "label": "桶名", "required": True, "placeholder": "如 my-bucket"}
_PREFIX_FIELD = {"key": "prefix", "label": "前缀", "placeholder": "留空为桶根目录，如 dir/"}
_REGION_FIELD = {"key": "region", "label": "区域",
                 "placeholder": f"留空用凭据里的（默认 {DEFAULT_REGION}）"}


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据完整时提供只读的对象存储查询）。"""
    data = data or {}
    if not (_field(data, "endpoint") and _field(data, "access_key") and _field(data, "secret_key")):
        return []
    return [
        {
            "id": "list_buckets", "name": "对象存储桶列表", "kind": "list",
            "group": "存储", "order": 30,
            "description": "S3 ListBuckets（只读；双击「预览」下钻到该桶的对象）",
            "columns": _BUCKET_COLUMNS, "row_actions": [_OBJECT_ROW_ACTION],
            "empty_text": "暂无对象存储桶",
        },
        {
            "id": "list_objects", "name": "对象浏览", "kind": "list",
            "group": "存储", "order": 40,
            "description": "ListObjectsV2（按 / 分隔列出文件与文件夹；双击可「进入」，右键可「下载」「打包下载」）",
            "columns": _OBJECT_COLUMNS,
            "fields": [_BUCKET_FIELD, _PREFIX_FIELD, _REGION_FIELD],
            "row_actions": _OBJECT_ROW_ACTIONS, "submit": "查询",
            "empty_text": f"该前缀下暂无对象（仅显示前 {_PAGE_SIZE} 个）",
        },
        {
            "id": "download_object", "name": "下载对象", "kind": "form",
            "group": "存储", "order": 50,
            "description": "下载桶内的单个对象（弹「另存为」；从对象列表右键「下载」进入会自动填好）",
            "fields": [
                _BUCKET_FIELD,
                {"key": "key", "label": "对象 Key", "required": True, "placeholder": "如 dir/file.txt"},
                _REGION_FIELD,
            ],
            "save_to": {"key": "path", "default_name": "$row.fileName"},
            "progress": True, "submit": "下载",
        },
        {
            "id": "download_prefix", "name": "打包下载（zip）", "kind": "form",
            "group": "存储", "order": 60,
            "description": (
                f"把该前缀下所有对象拉下来打成一个 zip（最多 {MAX_ZIP_OBJECTS} 个对象 / "
                f"{human_size(MAX_ZIP_BYTES)}，超出会明确报错而不静默截断）"
            ),
            "fields": [_BUCKET_FIELD, _PREFIX_FIELD, _REGION_FIELD],
            "save_to": {"key": "path", "default_name": "$row.folderName"},
            "progress": True, "submit": "打包下载",
        },
    ]


def run_action(action_id: str, params: dict, data: dict, progress=None, should_cancel=None) -> dict:
    """执行操作（全部只读，绝不产生远端变更）。``progress`` / ``should_cancel`` 为下载钩子。"""
    data = data or {}
    params = params or {}
    try:
        if not (_field(data, "endpoint") and _field(data, "access_key") and _field(data, "secret_key")):
            return {"ok": False, "error": "缺少服务端点或凭据"}
        if action_id == "list_buckets":
            return _run_list_buckets(data)
        if action_id == "list_objects":
            return _run_list_objects(data, params)
        if action_id == "download_object":
            return _run_download_object(data, params, progress, should_cancel)
        if action_id == "download_prefix":
            return _run_download_prefix(data, params, progress, should_cancel)
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def stream_action(action_id: str, params: dict, data: dict, on_delta,
                  on_progress=None, should_cancel=None) -> dict:
    """下载类操作的流式执行：边下边上报进度、并响应取消。"""
    if action_id in ("download_object", "download_prefix"):
        return run_action(action_id, params, data, on_progress, should_cancel)
    return run_action(action_id, params, data)


# ── 行构造 ────────────────────────────────────────────────

def _object_row_file(bucket: str, region: str, key: str, size_raw: str, modified: str) -> dict:
    return {
        "key": key, "type": "文件",
        "size": int(size_raw) if str(size_raw).isdigit() else size_raw,
        "lastModified": modified,
        "bucket": bucket, "region": region,
        "fileKey": key, "fileName": key.rstrip("/").rsplit("/", 1)[-1],
    }


def _object_row_folder(bucket: str, region: str, prefix: str) -> dict:
    name = prefix.rstrip("/").rsplit("/", 1)[-1] or "bucket"
    return {
        "key": prefix, "type": "文件夹", "size": "", "lastModified": "",
        "bucket": bucket, "region": region,
        "folderPath": prefix, "folderName": f"{name}.zip",
    }


# ── 列桶 / 列对象 ─────────────────────────────────────────

def _run_list_buckets(data: dict) -> dict:
    ep, error = _endpoint_info(data)
    if error:
        return {"ok": False, "error": error}
    region = _region(data)
    with _client(data) as client:
        url, headers = _signed_url(data, ep, "", "", None, region)
        resp = client.get(url, headers=headers)
    if resp.status_code != 200:
        return {"ok": False, "error": _failure(resp, data)}

    rows = []
    for bucket in xml_findall(xml_root(resp.text), "Bucket"):
        name = child_text(bucket, "Name")
        if not name:
            continue
        rows.append({
            "name": name,
            "creationDate": child_text(bucket, "CreationDate"),
            "region": child_text(bucket, "BucketRegion") or region,
        })
    return {"ok": True, "rows": rows, "columns": _BUCKET_COLUMNS,
            "row_actions": [_OBJECT_ROW_ACTION], "total": len(rows), "has_more": False}


def _run_list_objects(data: dict, params: dict) -> dict:
    ep, error = _endpoint_info(data)
    if error:
        return {"ok": False, "error": error}
    bucket, error = _bucket_name(params)
    if error:
        return {"ok": False, "error": error}
    region = _region(data, params)
    prefix = _field(params, "prefix")

    query: dict[str, str] = {"list-type": "2", "delimiter": "/", "max-keys": str(_PAGE_SIZE)}
    if prefix:
        query["prefix"] = prefix
    with _client(data) as client:
        url, headers = _signed_url(data, ep, bucket, "", query, region)
        resp = client.get(url, headers=headers)
    if resp.status_code != 200:
        return {"ok": False, "error": _failure(resp, data)}

    root = xml_root(resp.text)
    rows: list[dict] = []
    for node in xml_findall(root, "CommonPrefixes"):
        child = child_text(node, "Prefix")
        if child:
            rows.append(_object_row_folder(bucket, region, child))
    for content in xml_findall(root, "Contents"):
        key = child_text(content, "Key")
        if not key or key.endswith("/"):
            continue
        rows.append(_object_row_file(bucket, region, key,
                                     child_text(content, "Size"),
                                     child_text(content, "LastModified")))
    where = prefix or "桶根目录"
    return {"ok": True, "rows": rows, "columns": _OBJECT_COLUMNS,
            "row_actions": _OBJECT_ROW_ACTIONS, "total": len(rows),
            "empty_text": f"该前缀下暂无对象（{bucket} / {where}，仅显示前 {_PAGE_SIZE} 个）",
            "has_more": xml_text(root, "IsTruncated").lower() == "true"}


# ── 下载 / 打包下载 ───────────────────────────────────────

def _run_download_object(data: dict, params: dict, progress=None, should_cancel=None) -> dict:
    ep, error = _endpoint_info(data)
    if error:
        return {"ok": False, "error": error}
    bucket, error = _bucket_name(params)
    if error:
        return {"ok": False, "error": error}
    key = _field(params, "key")
    if not key:
        return {"ok": False, "error": "缺少对象 Key"}
    path = _field(params, "path")
    if not path:
        return {"ok": False, "error": "未指定保存路径"}
    region = _region(data, params)

    with _client(data) as client:
        url, headers = _signed_url(data, ep, bucket, key, None, region)
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code != 200:
                return {"ok": False, "error": _failure(resp, data)}
            ok, message = download_with_progress(resp, path, key, progress, should_cancel)
    if not ok:
        return {"ok": False, "error": message}
    return {"ok": True, "output": f"已保存到 {message}"}


def _list_all_objects(client, data: dict, ep: dict, bucket: str, region: str,
                      prefix: str, limit: int):
    """翻页枚举前缀下**全部**对象（打包用）。返回 ``(objects, over_limit, error)``。"""
    objects: list[tuple[str, int]] = []
    token = ""
    while True:
        query: dict[str, str] = {"list-type": "2", "max-keys": str(_ENUM_PAGE)}
        if prefix:
            query["prefix"] = prefix
        if token:
            query["continuation-token"] = token
        url, headers = _signed_url(data, ep, bucket, "", query, region)
        resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            return objects, False, _failure(resp, data)
        root = xml_root(resp.text)
        for content in xml_findall(root, "Contents"):
            key = child_text(content, "Key")
            if not key or key.endswith("/"):
                continue
            size_raw = child_text(content, "Size")
            objects.append((key, int(size_raw) if size_raw.isdigit() else 0))
            if len(objects) > limit:
                return objects, True, ""
        if xml_text(root, "IsTruncated").lower() != "true":
            break
        token = xml_text(root, "NextContinuationToken")
        if not token:
            break
    return objects, False, ""


def _zip_entry_name(key: str, prefix: str) -> str | None:
    rel = key[len(prefix):] if prefix and key.startswith(prefix) else key
    rel = rel.lstrip("/")
    parts = rel.split("/")
    if not rel or any(part in ("", ".", "..") for part in parts):
        return None
    return rel


def _run_download_prefix(data: dict, params: dict, progress=None, should_cancel=None) -> dict:
    """把前缀下所有对象并发拉取后打包成 zip。``progress`` 按对象个数上报；每对象前与每 chunk 查取消。"""
    ep, error = _endpoint_info(data)
    if error:
        return {"ok": False, "error": error}
    bucket, error = _bucket_name(params)
    if error:
        return {"ok": False, "error": error}
    path = _field(params, "path")
    if not path:
        return {"ok": False, "error": "未指定保存路径"}
    prefix = _field(params, "prefix")
    if not prefix:
        return {"ok": False, "error": "缺少前缀（文件夹），请在表单里填写，或从对象列表右键点击「打包下载」进入"}
    region = _region(data, params)

    with _client(data) as client:
        objects, over_limit, list_error = _list_all_objects(client, data, ep, bucket, region,
                                                            prefix, MAX_ZIP_OBJECTS)
        if list_error:
            return {"ok": False, "error": list_error}
        if over_limit:
            return {"ok": False, "error": (
                f"「{prefix}」下的对象数超过打包上限（{MAX_ZIP_OBJECTS} 个），已枚举前 "
                f"{MAX_ZIP_OBJECTS + 1} 个即停止。请换一个更小的前缀，或逐个下载。")}
        if not objects:
            return {"ok": False, "error": f"「{prefix}」下没有可下载的对象"}

        total = sum(size for _, size in objects)
        if total > MAX_ZIP_BYTES:
            return {"ok": False, "error": (
                f"「{prefix}」下共 {len(objects)} 个对象、{human_size(total)}，"
                f"超过单次打包上限 {human_size(MAX_ZIP_BYTES)}。请换一个更小的前缀，或逐个下载。")}

        tmpdir = tempfile.mkdtemp(prefix="vault_s3_zip_")
        part_path = path + ".part"
        failed: list[str] = []
        skipped: list[str] = []
        written = 0
        count = len(objects)
        done = 0
        if progress is not None:
            progress(0, count, f"开始打包 {count} 个对象")

        def _fetch(index: int, key: str):
            if should_cancel is not None and should_cancel():
                raise Cancelled
            local = os.path.join(tmpdir, f"{index}.bin")
            try:
                url, headers = _signed_url(data, ep, bucket, key, None, region)
                with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code != 200:
                        return key, "", f"{key} (HTTP {resp.status_code})"
                    with open(local, "wb") as handle:
                        for chunk in resp.iter_bytes(65536):
                            if should_cancel is not None and should_cancel():
                                raise Cancelled
                            handle.write(chunk)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - 单个对象失败不毁整包
                return key, "", f"{key} ({type(exc).__name__})"
            return key, local, ""

        try:
            with ThreadPoolExecutor(max_workers=_ZIP_WORKERS) as pool:
                futures = [pool.submit(_fetch, i, key) for i, (key, _size) in enumerate(objects)]
                with zipfile.ZipFile(part_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    for future in as_completed(futures):
                        key, local, fetch_error = future.result()
                        if should_cancel is not None and should_cancel():
                            raise Cancelled
                        done += 1
                        if progress is not None:
                            progress(done, count, f"{done}/{count} {key}")
                        if fetch_error:
                            failed.append(fetch_error)
                            continue
                        try:
                            entry = _zip_entry_name(key, prefix)
                            if entry is None:
                                skipped.append(f"{key}（路径不安全）")
                                continue
                            archive.write(local, arcname=entry)
                            written += 1
                        except Exception as exc:  # noqa: BLE001
                            failed.append(f"{key} ({type(exc).__name__})")
                        finally:
                            try:
                                os.remove(local)
                            except OSError:
                                pass
            if written == 0:
                raise RuntimeError("没有对象写入 zip")
            os.replace(part_path, path)
        except Cancelled:
            remove_quietly(part_path)
            return {"ok": False, "error": "打包已取消"}
        except Exception as exc:  # noqa: BLE001
            remove_quietly(part_path)
            detail = f"，失败 {len(failed)} 个" if failed else ""
            return {"ok": False, "error": f"打包失败（{type(exc).__name__}{detail}）"}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    lines = [f"已打包 {written} 个对象（{human_size(total)}）到 {path}", f"字节数（未压缩）：{total}"]
    if failed:
        lines.append(f"失败 {len(failed)} 个：" + "；".join(failed[:5]) + ("…" if len(failed) > 5 else ""))
    if skipped:
        lines.append(f"跳过 {len(skipped)} 个：" + "；".join(skipped[:5]) + ("…" if len(skipped) > 5 else ""))
    lines.append(f"zip 内为相对「{prefix}」的路径")
    return {"ok": True, "output": "\n".join(lines)}
