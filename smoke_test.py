"""端到端冒烟测试：不依赖网络，直接用 FastAPI TestClient 跑全流程。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".pylibs"))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

with TestClient(app) as client:
    # --- 健康检查 ---
    assert client.get("/health").json() == {"status": "ok"}

    # --- 1. 循环依赖被拒绝 ---
    r = client.post("/api/releases", json={
        "version": "bad",
        "nodes": [{"name": "x"}, {"name": "y"}],
        "dependencies": [
            {"from_node": "x", "to_node": "y"},
            {"from_node": "y", "to_node": "x"},
        ],
    })
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "cycle_detected"
    print("[OK] 循环依赖 -> 422 cycle_detected:", r.json()["error"].get("cycle_nodes"))

    # --- 2. 依赖边引用不存在节点 ---
    r = client.post("/api/releases", json={
        "version": "bad2",
        "nodes": [{"name": "a"}],
        "dependencies": [{"from_node": "a", "to_node": "ghost"}],
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_node"
    print("[OK] 未知节点引用 -> 422 unknown_node")

    # --- 3. 重复节点名 ---
    r = client.post("/api/releases", json={
        "version": "bad3",
        "nodes": [{"name": "a"}, {"name": "a"}],
        "dependencies": [],
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicate_node"
    print("[OK] 重复节点 -> 422 duplicate_node")

    # --- 4. 正常创建批次，验证拓扑分层 ---
    r = client.post("/api/releases", json={
        "version": "v1",
        "nodes": [
            {"name": "a", "node_type": "service"},
            {"name": "b"}, {"name": "c"}, {"name": "d"},
        ],
        "dependencies": [
            {"from_node": "a", "to_node": "c"},
            {"from_node": "b", "to_node": "c"},
            {"from_node": "c", "to_node": "d"},
        ],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    rid = body["id"]
    assert body["status"] == "pending"
    assert body["levels"] == [["a", "b"], ["c"], ["d"]], body["levels"]
    assert body["next_nodes"] == ["a", "b"]
    print("[OK] 创建批次", rid, "分层:", body["levels"])

    # 计划接口
    plan = client.get(f"/api/releases/{rid}/plan").json()
    assert plan["total_levels"] == 3
    assert plan["plan"] == [
        {"level": 0, "parallel_nodes": ["a", "b"]},
        {"level": 1, "parallel_nodes": ["c"]},
        {"level": 2, "parallel_nodes": ["d"]},
    ]
    print("[OK] 执行计划接口")

    # --- 5. 乱序回报：a 成功后直接回报 c（b 还没成功）---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 200 and r.json()["release"]["status"] == "running"

    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "c", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "out_of_order"
    assert r.json()["error"]["blockers"] == [{"node": "b", "status": "pending"}]
    print("[OK] 乱序回报 -> 409 out_of_order, blockers:", r.json()["error"]["blockers"])

    # --- 6. 跨版本回报 ---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "b", "status": "success", "version": "v9-old"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_mismatch"
    print("[OK] 跨版本回报 -> 409 version_mismatch")

    # --- 7. 重复回报 ---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_reported"
    print("[OK] 重复回报 -> 409 already_reported")

    # --- 8. b 成功 -> c 失败 -> 仅回滚 a、b（逆序）---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "b", "status": "success", "version": "v1"})
    assert r.status_code == 200

    r = client.post(f"/api/releases/{rid}/report", json={
        "node": "c", "status": "failed", "version": "v1",
        "error_code": "DEPLOY_TIMEOUT", "error_message": "健康检查超时",
    })
    assert r.status_code == 200, r.text
    fb = r.json()
    assert fb["result"] == "failed_and_rolled_back"
    assert fb["rollback_scope"] == "current_release_only"
    # 同层按 reported_at 倒序：b 后成功先回滚，a 随后
    assert [x["node"] for x in fb["rolled_back_nodes"]] == ["b", "a"], fb["rolled_back_nodes"]
    assert fb["resumable"] is True
    cp = fb["checkpoint"]
    assert cp["retry_endpoint"] == f"/api/releases/{rid}/retry"
    assert cp["restart_nodes"] == ["a", "b"] and cp["restart_level"] == 0
    print("[OK] c 失败 -> 回滚", [x["node"] for x in fb["rolled_back_nodes"]], "断点:", cp)

    st = fb["release"]
    assert st["status"] == "partially_rolled_back"
    assert st["failed_node"] == "c"
    node_status = {n["name"]: n["status"] for n in st["nodes"]}
    assert node_status == {"a": "rolled_back", "b": "rolled_back",
                           "c": "failed", "d": "pending"}, node_status

    # 回滚后不能直接继续回报
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "release_aborted"
    print("[OK] 回滚后继续回报 -> 409 release_aborted")

    # --- 9. 断点重试：failed/rolled_back 重置为 pending ---
    r = client.post(f"/api/releases/{rid}/retry")
    assert r.status_code == 200, r.text
    rb = r.json()
    assert sorted(rb["reset_nodes"]) == ["a", "b", "c"]
    assert rb["checkpoint"]["next_nodes"] == ["a", "b"]
    assert rb["release"]["status"] == "running"
    print("[OK] 断点重试 -> 重置", sorted(rb["reset_nodes"]), "next:", rb["checkpoint"]["next_nodes"])

    # 非失败状态不能重试
    r = client.post(f"/api/releases/{rid}/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "retry_not_allowed"
    print("[OK] 非失败状态重试 -> 409 retry_not_allowed")

    # --- 10. 重试后走完全部节点 -> succeeded ---
    for node in ("a", "b", "c", "d"):
        r = client.post(f"/api/releases/{rid}/report",
                        json={"node": node, "status": "success", "version": "v1"})
    assert r.status_code == 200
    assert r.json()["release"]["status"] == "succeeded"
    assert r.json()["release"]["progress"] == {"succeeded": 4, "total": 4, "completed": True}
    print("[OK] 重试后全部成功 -> succeeded")

    # 已完成批次不再接受回报
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "release_finished"
    print("[OK] 完成后回报 -> 409 release_finished")

    # --- 11. 第二批失败且此前无成功节点 -> failed（而非 partially_rolled_back），只影响本批次 ---
    r = client.post("/api/releases", json={
        "version": "v2",
        "nodes": [{"name": "solo"}],
        "dependencies": [],
    })
    rid2 = r.json()["id"]
    r = client.post(f"/api/releases/{rid2}/report",
                    json={"node": "solo", "status": "failed", "version": "v2"})
    assert r.json()["release"]["status"] == "failed"
    assert r.json()["rolled_back_nodes"] == []
    # 第一批仍然 succeeded —— 回滚严格限定本批次
    assert client.get(f"/api/releases/{rid}").json()["status"] == "succeeded"
    print("[OK] 无成功节点失败 -> failed；历史批次不受影响")

    # --- 12. 部分回滚：只回滚成功的 a，无关的 pending 节点 b 不受影响 ---
    r = client.post("/api/releases", json={
        "version": "v3",
        "nodes": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        "dependencies": [{"from_node": "a", "to_node": "c"}],  # level0=[a,b], level1=[c]
    })
    rid3 = r.json()["id"]
    assert client.post(f"/api/releases/{rid3}/report",
                       json={"node": "a", "status": "success", "version": "v3"}).status_code == 200
    r = client.post(f"/api/releases/{rid3}/report",
                    json={"node": "c", "status": "failed", "version": "v3"})
    fb3 = r.json()
    assert [x["node"] for x in fb3["rolled_back_nodes"]] == ["a"]
    assert fb3["checkpoint"]["restart_nodes"] == ["a"]  # b 仍 pending，不在重置列表
    ns3 = {n["name"]: n["status"] for n in fb3["release"]["nodes"]}
    assert ns3 == {"a": "rolled_back", "b": "pending", "c": "failed"}, ns3
    rt = client.post(f"/api/releases/{rid3}/retry").json()
    # 重试后 a 重新可执行，b 本来就 pending，二者同为 level0 断点
    assert rt["checkpoint"]["next_nodes"] == ["a", "b"]
    assert sorted(rt["reset_nodes"]) == ["a", "c"]
    print("[OK] 部分回滚 -> 仅回滚 [a]；重试断点 next=[a,b]，重置=[a,c]")

    # --- 13. 列表 & 404 ---
    items = client.get("/api/releases").json()["items"]
    assert {i["id"] for i in items} == {rid, rid2, rid3}
    assert client.get("/api/releases/nope").status_code == 404
    assert client.post("/api/releases/nope/report",
                       json={"node": "a", "status": "success", "version": "v"}).status_code == 404
    print("[OK] 列表接口 / 404")

    # --- 13b. estimated_seconds 校验：填写时必须为正整数 ---
    for bad in (0, -5):
        r = client.post("/api/releases", json={
            "version": f"bad-dur-{bad}",
            "nodes": [{"name": "a", "estimated_seconds": bad}],
        })
        assert r.status_code == 422, r.text
    print("[OK] estimated_seconds 为 0 / 负数 -> 422")

    # --- 13c. 关键路径分析：前向/后向 DP、零余量关键节点、字典序最小关键路径 ---
    r = client.post("/api/releases", json={
        "version": "v4",
        "nodes": [
            {"name": "svc-a", "estimated_seconds": 3},
            {"name": "svc-b", "estimated_seconds": 3},
            {"name": "svc-c", "estimated_seconds": 2},
            {"name": "svc-d", "estimated_seconds": 4},
            {"name": "svc-e", "estimated_seconds": 1},
        ],
        "dependencies": [
            {"from_node": "svc-a", "to_node": "svc-c"},
            {"from_node": "svc-b", "to_node": "svc-c"},
            {"from_node": "svc-c", "to_node": "svc-d"},
            {"from_node": "svc-a", "to_node": "svc-e"},
        ],
    })
    assert r.status_code == 201, r.text
    rid4 = r.json()["id"]
    dur4 = {n["name"]: n["estimated_seconds"] for n in r.json()["nodes"]}
    assert dur4 == {"svc-a": 3, "svc-b": 3, "svc-c": 2, "svc-d": 4, "svc-e": 1}, dur4

    plan4 = client.get(f"/api/releases/{rid4}/plan").json()
    s4 = plan4["schedule"]
    assert s4["total_duration_seconds"] == 9  # 3 + 2 + 4（最长路径）
    # svc-a->svc-c->svc-d 与 svc-b->svc-c->svc-d 并列最长，取节点名序列字典序最小者
    assert s4["critical_path"] == ["svc-a", "svc-c", "svc-d"], s4["critical_path"]
    assert s4["critical_nodes"] == ["svc-a", "svc-b", "svc-c", "svc-d"]
    assert s4["deadline_seconds"] is None and s4["feasible"] is True
    assert s4["deficit_seconds"] == 0
    sn = {n["name"]: n for n in s4["nodes"]}
    es_ls_slack = {k: (v["earliest_start"], v["latest_start"], v["total_slack"])
                   for k, v in sn.items()}
    assert es_ls_slack == {
        "svc-a": (0, 0, 0), "svc-b": (0, 0, 0), "svc-c": (3, 3, 0),
        "svc-d": (5, 5, 0), "svc-e": (3, 8, 5),
    }, es_ls_slack
    assert sn["svc-d"]["is_critical"] is True and sn["svc-e"]["is_critical"] is False
    print("[OK] 关键路径分析 total=9 path=", s4["critical_path"],
          "关键节点=", s4["critical_nodes"])

    # deadline 充足 / 恰好相等 -> feasible
    s = client.get(f"/api/releases/{rid4}/plan",
                   params={"deadline_seconds": 12}).json()["schedule"]
    assert s["feasible"] is True and s["deficit_seconds"] == 0
    assert s["deadline_seconds"] == 12
    s = client.get(f"/api/releases/{rid4}/plan",
                   params={"deadline_seconds": 9}).json()["schedule"]
    assert s["feasible"] is True and s["deficit_seconds"] == 0

    # deadline 不足 -> 仍返回完整分析，feasible=false + 缺口秒数；查询不改变批次状态
    before = client.get(f"/api/releases/{rid4}").json()
    s = client.get(f"/api/releases/{rid4}/plan",
                   params={"deadline_seconds": 7}).json()["schedule"]
    assert s["feasible"] is False and s["deficit_seconds"] == 2
    assert s["total_duration_seconds"] == 9
    assert s["critical_path"] == ["svc-a", "svc-c", "svc-d"]
    assert len(s["nodes"]) == 5
    after = client.get(f"/api/releases/{rid4}").json()
    assert after["status"] == before["status"] == "pending"
    assert after["updated_at"] == before["updated_at"]
    print("[OK] deadline=7 -> feasible=false, deficit=2；plan 查询为只读")

    # deadline_seconds 参数校验：负数 -> 422
    assert client.get(f"/api/releases/{rid4}/plan",
                      params={"deadline_seconds": -1}).status_code == 422
    print("[OK] deadline_seconds 为负数 -> 422")

    # --- 13d. 创建时未填工期 -> 按 0 处理（rid3 全部节点无工期） ---
    s3 = client.get(f"/api/releases/{rid3}/plan",
                    params={"deadline_seconds": 0}).json()["schedule"]
    assert s3["total_duration_seconds"] == 0 and s3["feasible"] is True
    assert all(n["estimated_seconds"] == 0 for n in s3["nodes"])
    assert s3["critical_path"] == ["a", "c"]  # 全零工期下字典序最小的最长链
    print("[OK] 未填工期批次按 0 处理: total=0, path=", s3["critical_path"])

# --- 14. 进程重启场景：对同一 SQLite 文件重新 init_db，数据仍在且表结构幂等 ---
from app import database  # noqa: E402

database.init_db(os.environ["DB_PATH"])
conn = database.get_conn()
count = conn.execute("SELECT COUNT(*) AS c FROM releases").fetchone()["c"]
assert count == 4, count
with database.lock():
    v1 = conn.execute("SELECT status, version FROM releases WHERE version='v1'").fetchone()
assert dict(v1) == {"status": "succeeded", "version": "v1"}
print("[OK] SQLite 重新初始化幂等，历史数据保留（4 个批次）")

# --- 15. 旧版数据库文件迁移：nodes 表无 estimated_seconds 列时自动补列，存量行按 0 ---
import sqlite3  # noqa: E402

_old_db = os.path.join(_tmp, "legacy.db")
_legacy = sqlite3.connect(_old_db)
_legacy.executescript("""
CREATE TABLE releases (
    id TEXT PRIMARY KEY, version TEXT NOT NULL, status TEXT NOT NULL,
    levels_json TEXT NOT NULL DEFAULT '[]', error_code TEXT, error_message TEXT,
    failed_node TEXT, failed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, name TEXT NOT NULL,
    node_type TEXT NOT NULL DEFAULT 'service', level INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
    reported_version TEXT, error_code TEXT, error_message TEXT, reported_at TEXT,
    updated_at TEXT NOT NULL, UNIQUE(release_id, name)
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL,
    from_node TEXT NOT NULL, to_node TEXT NOT NULL,
    UNIQUE(release_id, from_node, to_node)
);
CREATE TABLE node_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, node_name TEXT NOT NULL,
    event_type TEXT NOT NULL, version TEXT, detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
""")
_legacy.execute(
    "INSERT INTO releases (id, version, status, levels_json, created_at, updated_at)"
    " VALUES ('legacy1', 'v0', 'succeeded', '[[\"old-a\"]]', 't', 't')"
)
_legacy.execute(
    "INSERT INTO nodes (release_id, name, level, updated_at) VALUES ('legacy1', 'old-a', 0, 't')"
)
_legacy.commit()
_legacy.close()

database.init_db(_old_db)  # 触发幂等迁移
cols = {row["name"] for row in database.get_conn().execute("PRAGMA table_info(nodes)")}
assert "estimated_seconds" in cols

from app import service  # noqa: E402

legacy_plan = service.get_release_plan("legacy1")
assert legacy_plan["schedule"]["total_duration_seconds"] == 0
assert legacy_plan["schedule"]["nodes"][0]["estimated_seconds"] == 0
assert legacy_plan["schedule"]["critical_path"] == ["old-a"]
database.init_db(_old_db)  # 重复迁移幂等
print("[OK] 旧库自动补 estimated_seconds 列，存量批次工期按 0 处理")

print("\n全部测试通过 ✔")
