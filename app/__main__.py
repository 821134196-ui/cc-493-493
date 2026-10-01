"""本地启动入口:python3 -m app [--host 127.0.0.1] [--port 8000] [--reseed]"""
import argparse
import os
import sys

from .seed import seed
from .server import create_server

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def main(argv=None):
    parser = argparse.ArgumentParser(description="消防通道临停占道管理系统")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--db", default=os.environ.get("DB_PATH", os.path.join(DEFAULT_DIR, "firelane.db")))
    parser.add_argument("--photos", default=os.environ.get("PHOTO_DIR", os.path.join(DEFAULT_DIR, "photos")))
    parser.add_argument("--reseed", action="store_true", help="重置并写入演示数据")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    if args.reseed or not os.path.exists(args.db):
        seed(args.db, args.photos)

    httpd, _ = create_server(args.host, args.port, args.db, args.photos)
    print(f"消防通道临停占道管理系统已启动: http://{args.host}:{args.port}")
    print("演示账号: admin/admin123(值班负责人) guard1/guard123(1栋保安) "
          "guard4/guard123(1栋另一保安) guard2 guard3 / resident1/resident123(住户)")
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        return 0


if __name__ == "__main__":
    sys.exit(main())
