"""SQLite 连接管理与表结构（谱系 DAG）。

表设计：
- samples       样品主表，记录当前剩余体积 / 单位 / 来源操作号
- operations    操作流水（取样 / 分装 / 合并），操作号唯一，原始载荷与结果快照
- lineages      谱系有向边：parent_sample -> child_sample，child 消耗 parent
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from .config import BUSY_TIMEOUT_MS, DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS samples (
    code            TEXT PRIMARY KEY,
    initial_volume  REAL NOT NULL,
    remaining       REAL NOT NULL,
    unit            TEXT NOT NULL,
    created_by_op   TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (created_by_op) REFERENCES operations(op_no)
);

CREATE TABLE IF NOT EXISTS operations (
    op_no       TEXT PRIMARY KEY,
    op_type     TEXT NOT NULL CHECK (op_type IN ('sample', 'aliquot', 'merge')),
    payload     TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS lineages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    op_no            TEXT NOT NULL,
    parent_sample    TEXT NOT NULL,
    child_sample     TEXT NOT NULL,
    consumed_volume  REAL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (op_no) REFERENCES operations(op_no),
    FOREIGN KEY (parent_sample) REFERENCES samples(code),
    FOREIGN KEY (child_sample) REFERENCES samples(code),
    CHECK (consumed_volume IS NULL OR consumed_volume >= 0)
);

CREATE INDEX IF NOT EXISTS idx_lineages_parent ON lineages(parent_sample);
CREATE INDEX IF NOT EXISTS idx_lineages_child ON lineages(child_sample);
CREATE INDEX IF NOT EXISTS idx_operations_type ON operations(op_type);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开一个开启外键约束的 SQLite 连接。"""
    path = db_path or str(DB_PATH)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def init_db(db_path: str | None = None) -> None:
    """创建数据目录并初始化表结构 / 索引（幂等）。"""
    path = db_path or str(DB_PATH)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        # 多语句脚本需要 executescript；其中的 WAL 设置仅首次执行。
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接（FastAPI 自动识别生成器依赖并负责关闭）。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
