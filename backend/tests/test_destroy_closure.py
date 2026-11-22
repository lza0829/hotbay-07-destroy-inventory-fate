"""销毁留幽灵占键架构的闭合性测试。
每个用例独立临时库（function 级 fixture 重指 app.db.DB_PATH），互不污染。
覆盖：终态/素材段/目录占键/回执/徽章/互证六处联动、回执诚实、失败整体回滚、
并发销毁与登记串行化、幽灵回收、已销毁者不进互证元数据侧、禁止只改徽章。"""
from __future__ import annotations

import sys
import tempfile
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, "/workspace/backend")

from fastapi import HTTPException  # noqa: E402

from app import config, db as dbmod  # noqa: E402
from app.db import db, init_db, INVENTORY_STATE_GHOST, INVENTORY_STATE_LIVE  # noqa: E402
from app.services import retention, verify, partitions as part_svc  # noqa: E402


@pytest.fixture()
def fresh_db(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="hotbay_test_")) / "case.db"
    monkeypatch.setattr(config, "DB_PATH", d)
    monkeypatch.setattr(dbmod.config, "DB_PATH", d)
    init_db()
    with db() as conn:
        conn.execute(
            "INSERT INTO vaults(name, capacity, active) VALUES('测试片库间', 1000, 1)"
        )
    return d


def seed_one(part_key: str, age_days: int = 120, clips=("seg-01.bin", "seg-02.bin")):
    today = date.today()
    day = str(today - timedelta(days=age_days))
    clip_in = [
        {"object_key": f"reel/{part_key}/{c}", "bytes": 1000 + i, "note": c}
        for i, c in enumerate(clips)
    ]
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('营业日',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (today.isoformat(),),
        )
    return part_svc.create_partition(part_key, part_key, day, 1, "热档", f"reel/{part_key}/", clip_in)


def setup_policy(action: str = "销毁", max_age: int = 90) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO retention_policies(name,max_age_days,action,active) VALUES(?,?,?,1)",
            (f"策略-{action}-{max_age}-{uuid.uuid4().hex[:8]}", max_age, action),
        )
        return cur.lastrowid


def states(conn, pid):
    p = conn.execute("SELECT status FROM partitions WHERE id=?", (pid,)).fetchone()
    clip_n = conn.execute("SELECT COUNT(*) c FROM clips WHERE partition_id=?", (pid,)).fetchone()["c"]
    return p["status"], clip_n


@pytest.mark.usefixtures("fresh_db")
def test_destroy_leaves_ghosts_and_honest_receipt():
    pid = seed_one("P-ghost-1")["id"]
    pol = setup_policy()
    with db() as conn:
        keys = [r["object_key"] for r in conn.execute(
            "SELECT object_key FROM clips WHERE partition_id=?", (pid,))]

    result = retention.run_retention(pol)
    aff = [a for a in result["affected"] if a["partition_id"] == pid][0]

    # 回执：清单未清、幽灵数实计、绝不伪装已连清
    assert aff["manifest_cleared"] == 0
    assert aff["ghost_key_count"] == len(keys)
    assert result["mode"] == "留幽灵销毁"

    with db() as conn:
        status, clip_n = states(conn, pid)
        assert status == "已销毁"                      # 终态徽章
        assert clip_n == 0                             # 册面素材段已拆除
        rows = conn.execute(
            "SELECT state,destroy_run_id,destroyed_at FROM inventory_objects WHERE object_key IN (?,?)",
            tuple(keys),
        ).fetchall()
        assert len(rows) == len(keys)                  # 占键保留，未删除
        assert all(r["state"] == INVENTORY_STATE_GHOST for r in rows)
        assert all(r["destroy_run_id"] == aff["run_id"] for r in rows)
        assert all(r["destroyed_at"] for r in rows)
        run = conn.execute("SELECT * FROM destroy_runs WHERE id=?", (aff["run_id"],)).fetchone()
        assert run["manifest_cleared"] == 0 and run["ghost_key_count"] == len(keys)
        assert run["status"] == "已销毁"


@pytest.mark.usefixtures("fresh_db")
def test_verify_excludes_destroyed_and_ghosts_not_drift():
    seed_one("P-ghost-2")
    pol = setup_policy()
    retention.run_retention(pol)
    rep = verify.run_verify()
    # 全库仅幽灵：无真漂移，幽灵单列可核
    assert rep["status"] == "一致"
    assert rep["drift_count"] == 0
    assert rep["ghost_count"] == 2
    assert all(g["问题"] == "已销毁幽灵占键" for g in rep["ghosts"])


@pytest.mark.usefixtures("fresh_db")
def test_destroy_rolls_back_all_on_failure():
    pid = seed_one("P-rollback-1")["id"]
    pol = setup_policy()
    with db() as conn:
        before = {
            "p": states(conn, pid),
            "runs": conn.execute("SELECT COUNT(*) c FROM destroy_runs").fetchone()["c"],
        }
        # 制造未闭合：删掉一条目录占键，使幽灵数 != 素材段数
        victim = conn.execute(
            "SELECT object_key FROM clips WHERE partition_id=? LIMIT 1", (pid,)
        ).fetchone()["object_key"]
        conn.execute("DELETE FROM inventory_objects WHERE object_key=?", (victim,))
        before["inv"] = conn.execute(
            "SELECT COUNT(*) c FROM inventory_objects"
        ).fetchone()["c"]  # 删除已提交，以此为销毁前基线

    # 闭合校验在回执、幽灵标记写入之后才抛错，正可验证整笔回滚
    with pytest.raises(HTTPException) as ei:
        retention.run_retention(pol)
    assert ei.value.status_code == 500 and "未闭合" in ei.value.detail

    with db() as conn:
        assert states(conn, pid) == before["p"]                      # 分区仍在册
        assert conn.execute(
            "SELECT COUNT(*) c FROM inventory_objects WHERE state=?", (INVENTORY_STATE_GHOST,)
        ).fetchone()["c"] == 0                                       # 无幽灵残留
        assert conn.execute(
            "SELECT COUNT(*) c FROM inventory_objects"
        ).fetchone()["c"] == before["inv"]                           # 占键数回到销毁前
        assert conn.execute(
            "SELECT COUNT(*) c FROM destroy_runs"
        ).fetchone()["c"] == before["runs"]                          # 回执一并回滚
        assert conn.execute(
            "SELECT COUNT(*) c FROM clips WHERE partition_id=?", (pid,)
        ).fetchone()["c"] == 2                                       # 素材段未拆


@pytest.mark.usefixtures("fresh_db")
def test_concurrent_destroy_and_register_serialize():
    pid = seed_one("P-conc-1")["id"]
    pol = setup_policy()
    barrier = threading.Barrier(2)
    errors = []

    def destroyer():
        try:
            barrier.wait()
            retention.run_retention(pol)
        except Exception as e:  # noqa: BLE001
            errors.append(("destroy", e))

    def registrar():
        try:
            barrier.wait()
            today = date.today()
            part_svc.create_partition(
                "P-conc-new", "并发登记", str(today), 1, "热档", "reel/P-conc-new/",
                [{"object_key": "reel/P-conc-new/seg-01.bin", "bytes": 9}],
            )
        except Exception as e:  # noqa: BLE001
            errors.append(("register", e))

    t1 = threading.Thread(target=destroyer)
    t2 = threading.Thread(target=registrar)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, errors
    with db() as conn:
        # 销毁的分区完整闭合
        assert conn.execute("SELECT status FROM partitions WHERE id=?", (pid,)).fetchone()["status"] == "已销毁"
        assert conn.execute(
            "SELECT COUNT(*) c FROM clips WHERE partition_id=?", (pid,)).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM inventory_objects WHERE state=? AND destroy_run_id IS NOT NULL",
            (INVENTORY_STATE_GHOST,),
        ).fetchone()["c"] == 2
        # 新登记成功且其占键在册，未被销毁误伤
        nid = conn.execute("SELECT id FROM partitions WHERE part_key='P-conc-new'").fetchone()["id"]
        assert conn.execute(
            "SELECT state FROM inventory_objects WHERE object_key='reel/P-conc-new/seg-01.bin'"
        ).fetchone()["state"] == INVENTORY_STATE_LIVE
        assert conn.execute("SELECT COUNT(*) c FROM clips WHERE partition_id=?", (nid,)).fetchone()["c"] == 1


@pytest.mark.usefixtures("fresh_db")
def test_double_destroy_rejected_after_lock():
    pid = seed_one("P-double-1")["id"]
    pol = setup_policy()
    retention.run_retention(pol)
    # 已销毁分区不再是候选；直接调单分区销毁应被锁后复检拒绝
    with pytest.raises(HTTPException) as ei:
        with db(immediate=True) as conn:
            retention.destroy_partition(conn, pid, pol, 999)
    assert ei.value.status_code == 409


@pytest.mark.usefixtures("fresh_db")
def test_ghost_reclaimed_on_reregister_same_key():
    seed_one("P-reclaim-1", clips=("seg-09.bin",))
    gkey = "reel/P-reclaim-1/seg-09.bin"
    pol = setup_policy()
    retention.run_retention(pol)
    with db() as conn:
        assert conn.execute(
            "SELECT state FROM inventory_objects WHERE object_key=?", (gkey,)
        ).fetchone()["state"] == INVENTORY_STATE_GHOST

    today = date.today()
    part_svc.create_partition(
        "P-reclaim-2", "同键再登记", str(today), 1, "热档", "reel/P-reclaim-1/",
        [{"object_key": gkey, "bytes": 7777}],
    )
    with db() as conn:
        row = conn.execute(
            "SELECT state,bytes,destroy_run_id,destroyed_at FROM inventory_objects WHERE object_key=?",
            (gkey,),
        ).fetchone()
        assert row["state"] == INVENTORY_STATE_LIVE
        assert row["bytes"] == 7777
        assert row["destroy_run_id"] is None and row["destroyed_at"] is None
    # 回收后互证无漂移
    rep = verify.run_verify()
    assert rep["status"] == "一致" and rep["drift_count"] == 0


def test_no_mode_fork_in_signature():
    # 全局只贯彻一种口径：执行入口不得再接受 mode 分叉
    import inspect
    sig = inspect.signature(retention.run_retention)
    assert "mode" not in sig.parameters
