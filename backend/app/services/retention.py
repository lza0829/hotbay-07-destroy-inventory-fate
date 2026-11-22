from __future__ import annotations
from datetime import date
from fastapi import HTTPException
from app.db import db, INVENTORY_STATE_GHOST
from app.services.core import now, partition_row

# 全局唯一销毁口径：留幽灵占键。
# 终态已销毁时册面素材段（clips）拆除、分区终态置「已销毁」，
# 但湖对象目录占键（inventory_objects）保留并显式标「幽灵占键」，
# 回执 manifest_cleared 恒为 0 且 ghost_key_count 如实计数，禁止伪装已连清。
DESTROY_MODE = "留幽灵销毁"


def list_policies():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM retention_policies ORDER BY id").fetchall()]


def list_destroys():
    with db() as conn:
        rows = conn.execute(
            '''SELECT d.*, p.part_key, p.title, pol.name AS policy_name
               FROM destroy_runs d
               JOIN partitions p ON p.id=d.partition_id
               JOIN retention_policies pol ON pol.id=d.policy_id
               ORDER BY d.id DESC'''
        ).fetchall()
        return [dict(r) for r in rows]


def destroy_partition(conn, pid: int, policy_id: int, age: int) -> dict:
    """在调用方给定的写事务内销毁单个分区。
    清键顺序（同笔事务、同成败）：
      1. 先落销毁回执（占住流水号）
      2. 目录占键标幽灵（保留行，不删除）
      3. 册面素材段 clips 删除
      4. 分区终态置「已销毁」徽章
      5. 回执回填实计幽灵数，闭合校验
    任一步抛错由外层事务整体回滚，不允许只改徽章。"""
    # 取锁后复检，防并发双销毁造成半截占键
    p = partition_row(conn, pid)
    if p["status"] != "在册":
        raise HTTPException(409, detail=f"分区 {p['part_key']} 非在册，拒绝销毁")

    clip_keys = [r["object_key"] for r in conn.execute(
        "SELECT object_key FROM clips WHERE partition_id=?", (pid,)
    ).fetchall()]

    cur = conn.execute(
        "INSERT INTO destroy_runs(partition_id, policy_id, mode, status, created_at, note, "
        "manifest_cleared, ghost_key_count) VALUES (?,?,?,?,?,?,0,0)",
        (pid, policy_id, DESTROY_MODE, "已销毁", now(), f"片龄{age}天"),
    )
    run_id = cur.lastrowid
    ts = now()

    # 2. 目录占键保留并显式打幽灵标记（只命中本分区当前在册占键）
    conn.execute(
        "UPDATE inventory_objects SET state=?, destroyed_at=?, destroy_run_id=? "
        "WHERE object_key IN (SELECT object_key FROM clips WHERE partition_id=?) AND state='在册'",
        (INVENTORY_STATE_GHOST, ts, run_id, pid),
    )
    ghost_count = conn.execute(
        "SELECT COUNT(*) AS c FROM inventory_objects "
        "WHERE destroy_run_id=? AND state=?",
        (run_id, INVENTORY_STATE_GHOST),
    ).fetchone()["c"]
    # 闭合校验：标幽灵数必须与素材段数一致，否则判定未闭合并回滚整笔
    if ghost_count != len(clip_keys):
        raise HTTPException(
            500,
            detail=f"分区 {p['part_key']} 销毁未闭合：素材段{len(clip_keys)}段，幽灵占键{ghost_count}键",
        )

    # 3. 册面素材段拆除
    conn.execute("DELETE FROM clips WHERE partition_id=?", (pid,))
    # 4. 分区终态徽章
    conn.execute("UPDATE partitions SET status='已销毁' WHERE id=?", (pid,))

    # 5. 回执如实回填：清单未清（manifest_cleared=0），幽灵数实计
    conn.execute(
        "UPDATE destroy_runs SET ghost_key_count=? WHERE id=?",
        (ghost_count, run_id),
    )
    return {
        "partition_id": pid,
        "run_id": run_id,
        "status": "已销毁",
        "manifest_cleared": 0,
        "ghost_key_count": ghost_count,
    }


def run_retention(policy_id: int):
    # BEGIN IMMEDIATE：销毁写与登记写串行化，杜绝并发半截占键
    with db(immediate=True) as conn:
        pol = conn.execute(
            "SELECT * FROM retention_policies WHERE id=? AND active=1", (policy_id,)
        ).fetchone()
        if not pol:
            raise HTTPException(404, detail="保留策略不存在")
        biz = conn.execute("SELECT value FROM settings WHERE key='营业日'").fetchone()
        today = date.fromisoformat(biz["value"] if biz else date.today().isoformat())
        # 候选在取写锁后读取，避免锁外快照导致重复销毁
        parts = conn.execute("SELECT * FROM partitions WHERE status='在册'").fetchall()
        done = []
        for p in parts:
            age = (today - date.fromisoformat(p["release_day"])).days
            if age < pol["max_age_days"]:
                continue
            if pol["action"] == "只标过期":
                conn.execute("UPDATE partitions SET status='已过期' WHERE id=?", (p["id"],))
                cur = conn.execute(
                    "INSERT INTO destroy_runs(partition_id, policy_id, mode, status, created_at, note, "
                    "manifest_cleared, ghost_key_count) VALUES (?,?,?,?,?,?,0,0)",
                    (p["id"], policy_id, DESTROY_MODE, "已标记过期", now(), f"片龄{age}天"),
                )
                done.append({
                    "partition_id": p["id"], "run_id": cur.lastrowid,
                    "status": "已标记过期", "manifest_cleared": 0, "ghost_key_count": 0,
                })
                continue
            done.append(destroy_partition(conn, p["id"], policy_id, age))
        return {"policy": pol["name"], "mode": DESTROY_MODE, "affected": done}
