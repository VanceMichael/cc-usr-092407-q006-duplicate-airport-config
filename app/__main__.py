"""应用入口：装载配置、通过启动门禁并启动 HTTP 服务。"""

from __future__ import annotations

import json
import sys

from app.bootstrap import EXIT_CONFIG_CONFLICT, EXIT_CONFIG_ERROR, bootstrap
from app.config import Config
from app.errors import AppError, ConfigConflictError
from app.server import build_server


def main() -> int:
    config = Config.from_env()
    try:
        context = bootstrap(config)
    except ConfigConflictError as exc:
        # 配置与库中已登记的机场事实冲突：旧实例保持可读，本实例退出，
        # 不监听端口，也就不会接管流量。冲突明细随退出日志输出。
        print(f"startup refused: {exc.message}", file=sys.stderr, flush=True)
        if exc.details:
            print(
                "conflict details: "
                + json.dumps(exc.details, ensure_ascii=False, sort_keys=True),
                file=sys.stderr,
                flush=True,
            )
        return EXIT_CONFIG_CONFLICT
    except AppError as exc:
        print(f"startup refused: {exc.message}", file=sys.stderr, flush=True)
        if exc.details:
            print(
                "config error details: "
                + json.dumps(exc.details, ensure_ascii=False, sort_keys=True),
                file=sys.stderr,
                flush=True,
            )
        return EXIT_CONFIG_ERROR

    server = build_server(config.host, config.port, context.service)
    print(
        f"airport-disruption service listening on {config.host}:{config.port} "
        f"(db={config.db_path}, airports={len(context.airports)}, "
        f"flights={len(context.flights)}, digest={context.config_digest[:12]}…)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
