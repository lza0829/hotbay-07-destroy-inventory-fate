from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app import config

SCHEMA = '''
CREATE TABLE IF NOT EXISTS vaults (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  capacity INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS partitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  release_day TEXT NOT NULL,
  vault_id INTEGER NOT NULL,
  tier TEXT NOT NULL,
  object_prefix TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '在册',
  created_at TEXT NOT NULL,
  FOREIGN KEY(vault_id) REFERENCES vaults(id)
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  object_key TEXT NOT NULL UNIQUE,
  bytes INTEGER NOT NULL,
  entered_day TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);
CREATE TABLE IF NOT EXISTS migrate_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  from_tier TEXT NOT NULL,
  to_tier TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);
CREATE TABLE IF NOT EXISTS retention_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  max_age_days INTEGER NOT NULL,
  action TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS destroy_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  policy_id INTEGER NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  manifest_cleared INTEGER NOT NULL DEFAULT 0,
  ghost_key_count INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(partition_id) REFERENCES partitions(id),
  FOREIGN KEY(policy_id) REFERENCES retention_policies(id)
);
CREATE TABLE IF NOT EXISTS inventory_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  object_key TEXT NOT NULL UNIQUE,
  bytes INTEGER NOT NULL,
  vault_name TEXT NOT NULL,
  scanned_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT '在册',
  destroyed_at TEXT,
  destroy_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS verify_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  drift_count INTEGER NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
'''

# 全局唯一销毁口径：留幽灵占键。目录占键 state 取值只允许这两种。
INVENTORY_STATE_LIVE = '在册'
INVENTORY_STATE_GHOST = '幽灵占键'


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 并发写时等待锁而非立刻 database is locked
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db(immediate: bool = False):
    """事务边界。immediate=True 时以 BEGIN IMMEDIATE 先取写锁，
    保证销毁/登记的检查与写入在同一笔串行事务内，失败整体回滚。"""
    conn = connect()
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """对旧库做轻量加列迁移，幂等。"""
    inv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(inventory_objects)")}
    if "state" not in inv_cols:
        conn.execute(
            f"ALTER TABLE inventory_objects ADD COLUMN state TEXT NOT NULL DEFAULT '{INVENTORY_STATE_LIVE}'"
        )
    if "destroyed_at" not in inv_cols:
        conn.execute("ALTER TABLE inventory_objects ADD COLUMN destroyed_at TEXT")
    if "destroy_run_id" not in inv_cols:
        conn.execute("ALTER TABLE inventory_objects ADD COLUMN destroy_run_id INTEGER")

    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(destroy_runs)")}
    if "manifest_cleared" not in run_cols:
        conn.execute("ALTER TABLE destroy_runs ADD COLUMN manifest_cleared INTEGER NOT NULL DEFAULT 0")
    if "ghost_key_count" not in run_cols:
        conn.execute("ALTER TABLE destroy_runs ADD COLUMN ghost_key_count INTEGER NOT NULL DEFAULT 0")
    _backfill_legacy(conn)


def _backfill_legacy(conn: sqlite3.Connection) -> None:
    """旧库回执与占键据实归一到「留幽灵占键」口径，幂等。
    - 旧「连目录销毁」：清单确已清，回执标已连清。
    - 旧「只删元数据」：册面已毁、占键仍在，按幽灵占键补标记并挂回回执，
      模式名归一为当前唯一口径；回执如实记清单未清，禁止伪装。"""
    conn.execute(
        "UPDATE destroy_runs SET manifest_cleared=1, ghost_key_count=0 "
        "WHERE mode='连目录销毁' AND manifest_cleared=0"
    )
    legacy = conn.execute(
        '''SELECT d.id AS run_id, d.partition_id, p.object_prefix, d.created_at
           FROM destroy_runs d JOIN partitions p ON p.id=d.partition_id
           WHERE d.mode='只删元数据' AND d.status='已销毁' '''
    ).fetchall()
    for r in legacy:
        like = r["object_prefix"].replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        cur = conn.execute(
            "UPDATE inventory_objects SET state=?, destroyed_at=?, destroy_run_id=? "
            "WHERE destroy_run_id IS NULL AND state='在册' "
            "AND object_key LIKE ? ESCAPE '!'",
            (INVENTORY_STATE_GHOST, r["created_at"], r["run_id"], like),
        )
        conn.execute(
            "UPDATE destroy_runs SET mode='留幽灵销毁', manifest_cleared=0, ghost_key_count=? WHERE id=?",
            (cur.rowcount, r["run_id"]),
        )


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
