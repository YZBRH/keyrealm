"""仪表盘聚合接口测试（独立沙箱，不联网）。

覆盖：状态 / 新鲜度 / 类别 / 分组 / 风险 / 最近验证 / 系统状态，
并确认返回体不夹带凭据明文与密文；锁库后仍可读（只读聚合）。

运行：.venv/bin/python tests/test_dashboard.py
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-dash-"))

from backend import database as db  # noqa: E402

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "credentials.db"
db.init_db()

from backend import api, plugin_manager, tasks  # noqa: E402

# 任务注册表默认落在真实 data 目录，测试里改到沙箱
tasks._STORE = SANDBOX / "tasks.json"
tasks._tasks = {}
tasks._cancel_events = {}
tasks._loaded = True


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


def _make_credential(name, group_id, status, verified, message=""):
    cred = api.create_credential(
        name=name, credential_type="api_tavily",
        data={"api_key": f"tvly-{name}"}, group_id=group_id, auto_verify=False,
    )
    detail = {"status": status, "message": message, "account_info": None, "duration_ms": 5}
    db.update_credential(
        cred["id"], status=status, status_detail=detail,
        last_verified_at=verified, last_valid_at=verified if status == "valid" else None,
    )
    return cred["id"]


def setup_module():
    plugin_manager.sync_plugins()
    assert api.setup_master_password("TestPass!123")["ok"] is True
    group_a = api.create_group("生产", "", "#a3e635")
    group_b = api.create_group("测试", "", "#22d3ee")
    _make_credential("fresh-valid", group_a["id"], "valid", _iso(timedelta(hours=1)), "验证通过")
    _make_credential("bad-key", group_a["id"], "invalid", _iso(timedelta(hours=2)), "401 Unauthorized")
    _make_credential("never-checked", group_b["id"], "error", None, "连接超时")
    _make_credential("never-unknown", group_b["id"], "unknown", None)
    _make_credential("stale", None, "unknown", _iso(timedelta(days=10)))


def teardown_module():
    api.lock_vault()
    shutil.rmtree(SANDBOX, ignore_errors=True)


def test_overview_counts_and_shapes():
    data = api.dashboard_overview()
    assert data["ok"] is True

    assert data["totals"]["credentials"] == 5
    assert data["totals"]["groups"] == 2
    assert data["totals"]["plugins"] > 0

    assert data["status"]["valid"] == 1
    assert data["status"]["invalid"] == 1
    assert data["status"]["error"] == 1
    assert data["status"]["unknown"] == 2
    assert data["health"]["valid"] == 1
    assert data["health"]["total"] == 5
    assert abs(data["health"]["rate"] - 0.2) < 1e-9

    # fresh-valid / bad-key 都在 1 天内；stale 是 10 天；never-checked 从未验证
    assert data["freshness"] == {"never": 2, "day": 2, "week": 0, "month": 1, "older": 0}

    categories = {item["category"]: item for item in data["categories"]}
    assert categories["api"]["count"] == 5
    assert categories["api"]["name"]
    assert "api_tavily" in categories["api"]["types"]


def test_groups_risks_and_recent():
    data = api.dashboard_overview()
    groups = {item["id"]: item for item in data["groups"]}
    assert groups[None]["count"] == 1
    assert sum(item["count"] for item in data["groups"]) == 5

    risks = data["risks"]
    assert [item["severity"] for item in risks] == [0, 1, 2, 3]
    assert risks[0]["name"] == "bad-key"
    assert "401" in risks[0]["reason"]
    assert risks[2]["name"] == "never-unknown"
    assert risks[2]["reason"] == "从未验证"
    assert "天未验证" in risks[3]["reason"]

    recent = data["recent"]
    assert [item["name"] for item in recent][:2] == ["fresh-valid", "bad-key"]
    assert all(item["at"] for item in recent)


def test_tasks_schedules_and_system():
    data = api.dashboard_overview()
    assert set(data["tasks"]) == {"running", "total", "items"}
    assert data["tasks"]["running"] == 0
    assert set(data["schedules"]) == {"total", "enabled", "next_run_at"}
    system = data["system"]
    assert {"mcp", "proxy", "tls_version", "anonymous"} <= set(system)
    assert "running" in system["mcp"]
    assert "display" in system["proxy"]


def test_readable_while_locked_and_no_secrets():
    api.lock_vault()
    data = api.dashboard_overview()
    assert data["totals"]["credentials"] == 5

    # 聚合返回体不能出现凭据明文 / 密文 / 数据字段
    blob = json.dumps(data, ensure_ascii=False)
    for forbidden in ("data_plain", "data_encrypted", "tvly-"):
        assert forbidden not in blob


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    setup_module()
    passed = failed = 0
    try:
        for test in tests:
            try:
                test()
                print(f"  PASS  {test.__name__}")
                passed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL  {test.__name__}: {exc}")
                failed += 1
    finally:
        teardown_module()
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
