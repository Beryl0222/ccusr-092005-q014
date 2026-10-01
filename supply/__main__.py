"""命令行入口：python -m supply [--db supply.db] [--seed fixtures/seed.json] [--port 8080]"""
from __future__ import annotations

import argparse
from pathlib import Path

from .db import Database
from .httpapi import create_server
from .platform import SupplyPlatform


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="重点药品生产监测与调配后端")
    parser.add_argument("--db", default="supply.db", help="SQLite 路径，默认 supply.db")
    parser.add_argument("--seed", default="fixtures/seed.json", help="种子数据 JSON")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reset", action="store_true", help="删除已有库文件后重建")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if args.reset and db_path.exists():
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            side = db_path.with_name(db_path.name + suffix)
            if side.exists():
                side.unlink()

    fresh = not db_path.exists()
    db = Database(db_path)
    if fresh:
        db.initialize()
    platform = SupplyPlatform(db)
    if fresh and args.seed:
        result = platform.load_seed(args.seed)
        print(f"已装载种子：{result['loaded']}")

    server = create_server(platform, args.host, args.port, verbose=args.verbose)
    print(f"服务监听 http://{args.host}:{args.port}（Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        db.close()


if __name__ == "__main__":
    main()
