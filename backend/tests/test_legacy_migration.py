"""旧库到「留幽灵占键」口径的迁移归一测试。
手工构造无新列的旧 schema 与两种历史销毁模式，跑 init_db 后验证据实回填、不伪装。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, "/workspace/backend")

from app import config  # noqa: E402
from app.db import db, init_db, INVENTORY_STATE_GHOST, INVENTORY_STATE_LIVE  # noqa: E402
from app.services import verify  # noqa: E402

OLD_SCHEMA = '''
CREATE TABLE vaults (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  capacity INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE partitions (id INTEGER PRIMARY KEY AUTOINCREMENT, part_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, release_day TEXT NOT NULL, vault_id INTEGER NOT NULL, tier TEXT NOT NULL,
  object_prefix TEXT NOT NULL, status TEXT NOT NULL DEFAULT '在册', created_at TEXT NOT NULL);
CREATE TABLE clips (id INTEGER PRIMARY KEY AUTOINCREMENT, partition_id INTEGER NOT NULL,
  object_key TEXT NOT NULL UNIQUE, bytes INTEGER NOT NULL, entered_day TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE TABLE migrate_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, partition_id INTEGER NOT NULL,
  from_tier TEXT NOT NULL, to_tier TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
  finished_at TEXT, note TEXT NOT NULL DEFAULT '');
CREATE TABLE retention_policies (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  max_age_days INTEGER NOT NULL, action TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE destroy_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, partition_id INTEGER NOT NULL,
  policy_id INTEGER NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '');
CREATE TABLE inventory_objects (id INTEGER PRIMARY KEY AUTOINCREMENT, object_key TEXT NOT NULL UNIQUE,
  bytes INTEGER NOT NULL, vault_name TEXT NOT NULL, scanned_at TEXT NOT NULL);
CREATE TABLE verify_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,
  drift_count INTEGER NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


@pytest.fixture()
def legacy_db(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="hotbay_legacy_")) / "old.db"
    monkeypatch.setattr(config, "DB_PATH", d)
    import app.db as dbmod
    monkeypatch.setattr(dbmod.config, "DB_PATH", d)
    with db() as conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute("INSERT INTO vaults(id,name,capacity,active) VALUES(1,'旧库片间',10,1)")
        conn.execute("INSERT INTO retention_policies(id,name,max_age_days,action,active) "
                     "VALUES(1,'旧策略',90,'销毁',1)")
        # 分区 A：旧「只删元数据」—— clips 已删、占键仍在
        conn.execute("INSERT INTO partitions(id,part_key,title,release_day,vault_id,tier,"
                     "object_prefix,status,created_at) VALUES (1,'P-A','甲','2026-01-01',1,"
                     "'热档','reel/A/','已销毁','2026-04-01T00:00:00')")
        for i in (1, 2):
            ok = f"reel/A/seg-{i}.bin"
            conn.execute("INSERT INTO inventory_objects(object_key,bytes,vault_name,scanned_at) "
                         "VALUES(?,?,?,?)", (ok, 1000 + i, "旧库片间", "2026-01-01T00:00:00"))
        conn.execute("INSERT INTO destroy_runs(partition_id,policy_id,mode,status,created_at,note) "
                     "VALUES(1,1,'只删元数据','已销毁','2026-04-01T10:00:00','旧')")
        # 分区 B：旧「连目录销毁」—— clips 与占键都已清
        conn.execute("INSERT INTO partitions(id,part_key,title,release_day,vault_id,tier,"
                     "object_prefix,status,created_at) VALUES (2,'P-B','乙','2026-01-01',1,"
                     "'热档','reel/B/','已销毁','2026-04-02T00:00:00')")
        conn.execute("INSERT INTO destroy_runs(partition_id,policy_id,mode,status,created_at,note) "
                     "VALUES(2,1,'连目录销毁','已销毁','2026-04-02T10:00:00','旧')")
    return d


@pytest.mark.usefixtures("legacy_db")
def test_legacy_migration_normalizes_both_modes():
    init_db()
    with db() as conn:
        # A 的占键据实转幽灵并挂回原回执
        ghosts = conn.execute(
            "SELECT object_key, state, destroy_run_id, destroyed_at FROM inventory_objects "
            "WHERE object_key LIKE 'reel/A/%'"
        ).fetchall()
        assert len(ghosts) == 2
        assert all(g["state"] == INVENTORY_STATE_GHOST for g in ghosts)
        assert all(g["destroy_run_id"] == 1 for g in ghosts)
        assert all(g["destroyed_at"] == "2026-04-01T10:00:00" for g in ghosts)
        # A 回执：模式归一、清单未清、幽灵数实计，不伪装
        run_a = conn.execute("SELECT * FROM destroy_runs WHERE id=1").fetchone()
        assert run_a["mode"] == "留幽灵销毁"
        assert run_a["manifest_cleared"] == 0 and run_a["ghost_key_count"] == 2
        # B 回执：据实标已连清，无幽灵
        run_b = conn.execute("SELECT * FROM destroy_runs WHERE id=2").fetchone()
        assert run_b["manifest_cleared"] == 1 and run_b["ghost_key_count"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM inventory_objects WHERE object_key LIKE 'reel/B/%'"
        ).fetchone()["c"] == 0

    # 迁移后互证：幽灵不计漂移，且全库无在册素材侧，结论一致
    rep = verify.run_verify()
    assert rep["status"] == "一致" and rep["drift_count"] == 0
    assert rep["ghost_count"] == 2


@pytest.mark.usefixtures("legacy_db")
def test_legacy_migration_is_idempotent():
    init_db()
    init_db()  # 再跑一次不应重复计数或报错
    with db() as conn:
        run_a = conn.execute("SELECT * FROM destroy_runs WHERE id=1").fetchone()
        assert run_a["ghost_key_count"] == 2
        assert conn.execute(
            "SELECT COUNT(*) c FROM inventory_objects WHERE state=?", (INVENTORY_STATE_GHOST,)
        ).fetchone()["c"] == 2
