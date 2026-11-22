from __future__ import annotations
import json
from app.db import db, INVENTORY_STATE_GHOST, INVENTORY_STATE_LIVE
from app.services.core import now


def inventory_list():
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM inventory_objects ORDER BY object_key"
        ).fetchall()]


def run_verify():
    """互证可读集合口径（留幽灵占键架构）：
    - 元数据侧只认真在册/已过期分区的素材段；已销毁者不得再进互证元数据侧。
    - 目录侧拆为在册占键与幽灵占键：幽灵是销毁留下的预期痕迹，单独暴露，
      不计入漂移；只有在册占键参与真漂移比对。"""
    with db() as conn:
        meta_keys = {
            r["object_key"]: r["bytes"]
            for r in conn.execute(
                '''SELECT c.object_key, c.bytes FROM clips c
                   JOIN partitions p ON p.id=c.partition_id
                   WHERE p.status IN ('在册','已过期')'''
            ).fetchall()
        }
        live_rows = conn.execute(
            "SELECT object_key, bytes FROM inventory_objects WHERE state=?",
            (INVENTORY_STATE_LIVE,),
        ).fetchall()
        inv_keys = {r["object_key"]: r["bytes"] for r in live_rows}
        ghost_rows = conn.execute(
            "SELECT object_key, destroy_run_id FROM inventory_objects WHERE state=?",
            (INVENTORY_STATE_GHOST,),
        ).fetchall()

        only_meta = sorted(set(meta_keys) - set(inv_keys))
        only_inv = sorted(set(inv_keys) - set(meta_keys))
        size_drift = sorted(
            k for k in set(meta_keys) & set(inv_keys) if meta_keys[k] != inv_keys[k]
        )
        drifts = []
        for k in only_meta:
            drifts.append({"对象键": k, "问题": "元数据有、目录无"})
        for k in only_inv:
            drifts.append({"对象键": k, "问题": "目录有、元数据无"})
        for k in size_drift:
            drifts.append({"对象键": k, "问题": "字节不一致"})
        # 幽灵单独成列，可核但不污染漂移结论
        ghosts = [
            {"对象键": r["object_key"], "问题": "已销毁幽灵占键", "销毁流水": r["destroy_run_id"]}
            for r in sorted(ghost_rows, key=lambda r: r["object_key"])
        ]
        status = "一致" if not drifts else "漂移"
        detail = {"漂移": drifts, "幽灵占键": ghosts}
        cur = conn.execute(
            "INSERT INTO verify_runs(status, drift_count, detail, created_at) VALUES (?,?,?,?)",
            (status, len(drifts), json.dumps(detail, ensure_ascii=False), now()),
        )
        return {
            "run_id": cur.lastrowid,
            "status": status,
            "drift_count": len(drifts),
            "drifts": drifts,
            "ghost_count": len(ghosts),
            "ghosts": ghosts,
            "meta_count": len(meta_keys),
            "inventory_count": len(inv_keys),
        }


def list_verify_runs():
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM verify_runs ORDER BY id DESC"
        ).fetchall()]
