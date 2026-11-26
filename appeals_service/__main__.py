from __future__ import annotations

import argparse
import json
from pathlib import Path

from .api import serve
from .seed import seed_service
from .service import AppealsService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m appeals_service")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动 JSON HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--state", default="var/state.json")
    p_serve.add_argument("--seed", action="store_true", help="若案件为空则载入合成样例")

    p_seed = sub.add_parser("seed", help="把合成交换材料写入 state 文件")
    p_seed.add_argument("--state", default="var/state.json")

    p_demo = sub.add_parser("demo", help="在内存中跑通关键流程并打印摘要")
    p_demo.add_argument("--state", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "seed":
        path = Path(args.state)
        svc = AppealsService(str(path))
        seed_service(svc)
        print(json.dumps({"seeded": True, "state": str(path), "cases": list(svc.state["cases"])}, ensure_ascii=False))
        return 0

    if args.command == "demo":
        from .demo import run_demo

        result = run_demo(args.state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # 默认 serve
    state = getattr(args, "state", "var/state.json")
    svc = AppealsService(state)
    if getattr(args, "seed", False) and not svc.state["cases"]:
        seed_service(svc)
    httpd = serve(svc, args.host, args.port)
    print(f"appeals service on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
