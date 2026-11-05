"""凭据**数据的格式升级**——在解锁后把老格式的数据转成当前格式。

它属于「**数据结构变化**」的一部分：数据结构变了（含**只是凭据格式变了**）就要提升
``version.SCHEMA_VERSION`` 并在 ``scripts/migrations/`` 留一跳脚本（哪怕表结构没动，
见 ``m_1.0.0__to_1.1.0.py``）。那一跳里：
**表结构部分**由迁移脚本在更新交棒阶段用 SQL 完成；**数据部分**落到这里——因为凭据的敏感
字段是**加密**的，交棒阶段（应用已退出）没有主密码，读不出也改不了那些值。

**同一个 ``SCHEMA_VERSION`` 就是同一套数据结构**，不另立版本号。本模块只负责把数据补到位：
- 更新 / 换版后：**解锁时**把库里每条凭据过一遍规则（``api.run_data_upgrades``）——
  规则**幂等**，所以不记录"做到哪"，每次启动过一次即可（且只处理**有规则的类型**）；
- 导入备份：落库前把老格式转成当前格式（``backup.import_backup``）。

**规矩**：规则**只在这里维护**——插件应只描述**当前**格式、不写双格式兼容，框架其它地方
也不得为某个插件做格式容忍。规则必须**幂等**（重复跑无副作用）。
"""
from __future__ import annotations

from typing import Callable, Optional

# 规则：(credential_type 或 "*", 把老格式数据转成当前格式的函数)
# 函数收到**解密后的完整 data**，返回升级后的 data（无变化就原样返回）。
_Rule = Callable[[dict], dict]
_RULES: list[tuple[str, _Rule]] = []

# 旧凭据类型 → 当前类型。用于**导入旧备份**与读取旧的插件状态文件；
# DB 里存量凭据的类型由 scripts/migrations/ 的 SQL 迁移完成。
_TYPE_ALIASES: dict[str, str] = {
    "api_baidu_face": "api_baidu_ai",
}


def canonical_type(credential_type: str) -> str:
    """把旧 credential_type 归一为当前 id（幂等；未知类型原样返回）。"""
    ct = str(credential_type or "")
    return _TYPE_ALIASES.get(ct, ct)


def _rule(credential_type: str):
    """注册一条数据升级规则（模块加载时执行）。"""
    def deco(fn: _Rule) -> _Rule:
        _RULES.append((credential_type, fn))
        return fn
    return deco


@_rule("llm_wenxin")
def _wenxin_split_key(data: dict) -> dict:
    """文心 v1.1.0 起把凭证拆成 ``api_key`` + ``secret_key`` 两个字段
    （旧版只有一个 ``api_key``、要求拼成 ``API_KEY|SECRET_KEY``）。"""
    api = str(data.get("api_key") or "").strip()
    if "|" not in api or str(data.get("secret_key") or "").strip():
        return data                                  # 已是新格式（或本就没有 |），原样返回
    client_id, _, secret = api.partition("|")
    return {**data, "api_key": client_id.strip(), "secret_key": secret.strip()}


@_rule("api_baidu_ai")
def _baidu_ai_default_auth_mode(data: dict) -> dict:
    """百度 AI 平台 v1.1.0 起显式记录 ``auth_mode``；旧数据没有该字段时按 AK/SK 处理。"""
    if str(data.get("auth_mode") or "").strip():
        return data
    return {**data, "auth_mode": "ak_sk"}


@_rule("mail_smtp")
def _smtp_drop_legacy_tls(data: dict) -> dict:
    """邮箱 1.1.0 起「允许旧版 TLS」并入框架的「TLS 版本」下拉（保留键 ``_tls_version``）：
    删掉旧的 ``allow_legacy_tls``——不再由它决定。原先勾过的人，改在凭据的「TLS 版本」里
    固定成 ``1.1`` / ``1.0``（旧版握手失败的报错也会这样引导）。幂等：键不在就原样返回。"""
    if "allow_legacy_tls" not in data:
        return data
    return {k: v for k, v in data.items() if k != "allow_legacy_tls"}


def upgrade_credential(credential_type: str, data: dict) -> dict:
    """把一条凭据的数据升级到**当前格式**（幂等）。没有适用规则时原样返回。"""
    out = data or {}
    for ct, rule in _RULES:
        if ct == "*" or ct == credential_type:
            out = rule(out)
    return out


def rule_types() -> Optional[set[str]]:
    """有升级规则的 ``credential_type`` 集合；含**通配规则**（``"*"``）时返回 ``None``（表示全部）。

    调用方据此把待处理的凭据**限定在相关类型**上——没规则的插件条目连解密都不必要。
    """
    if any(ct == "*" for ct, _ in _RULES):
        return None
    return {ct for ct, _ in _RULES}
