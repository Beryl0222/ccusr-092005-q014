"""数据库连接与建库。

并发安全策略：
* 每个进程一个 *写连接*，所有写事务以 ``BEGIN IMMEDIATE`` 立即取得写锁，
  两地区同小时并发申请在 SQLite 层串行化，靠约束 + 行级重算决定成败，
  而不是靠应用层“先查后写”的乐观假设。
* 读连接可多开，``PRAGMA foreign_keys=ON`` 对每个连接单独生效。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _connect(path: str | Path) -> sqlite3.Connection:
    # 写连接在多线程 HTTP 服务器内共享，由 Database._write_lock 串行化，
    # 因此关闭 same-thread 检查。
    conn = sqlite3.connect(
        str(path), timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


class Database:
    """封装单一写连接 + 读连接工厂。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._write_lock = threading.RLock()
        self._writer = _connect(self.path)
        if self.path == ":memory:":
            # 内存库需要让其他连接看到同一份数据：测试中统一走写连接。
            self._shared_memory = True
        else:
            self._shared_memory = False

    @property
    def write_lock(self) -> threading.RLock:
        return self._write_lock

    def writer(self) -> sqlite3.Connection:
        return self._writer

    def reader(self) -> sqlite3.Connection:
        """返回一个读连接。内存库直接复用写连接（单测场景）。"""
        if self._shared_memory:
            return self._writer
        return _connect(self.path)

    def initialize(self) -> None:
        with open(SCHEMA_PATH, encoding="utf-8") as fh:
            self._writer.executescript(fh.read())

    def close(self) -> None:
        self._writer.close()

    def begin_immediate(self):
        """显式开启立即写事务的上下文管理器。

        崩溃恢复语义：事务未 COMMIT 则进程死后 WAL/回滚保证其全部撤销；
        已 COMMIT 的幂等键保证重放不重复入账。
        """
        return _ImmediateTransaction(self._writer, self._write_lock)


class _ImmediateTransaction:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
        finally:
            self._lock.release()
