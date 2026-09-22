"""应用配置。"""
from __future__ import annotations

import os
from pathlib import Path

# SQLite 数据库文件路径，可用环境变量 SAMPLE_DB_PATH 覆盖。
DB_PATH = Path(os.environ.get("SAMPLE_DB_PATH", "/data/samples.db"))

# 浮点余量比较容差：体积差小于 EPSILON 视为相等（避免浮点误差导致误判）。
EPSILON = 1e-9

# SQLite 写锁等待毫秒数。
BUSY_TIMEOUT_MS = 10_000
