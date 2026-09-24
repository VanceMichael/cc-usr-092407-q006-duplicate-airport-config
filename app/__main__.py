"""应用入口：校验配置、过数据库门禁，然后启动 HTTP 服务。

退出语义：

* 配置/夹具文件本身非法（重复三字码、非字符串名称、非法缓冲、语义重复时区、
  坏航班引用等）：打印带定位的确定性错误并以退出码 2 终止。此时尚未打开
  数据库，不会创建半初始化数据库。
* 数据库门禁冲突（配置与已登记机场事实矛盾）：进程不退出，启动一个"存活但
  未就绪"的诊断实例——``/healthz`` 为 ok，``/readyz`` 与 ``/api`` 为 503，
  旧数据保持可读、不被修改，等待运维回滚或更正配置。
"""

from __future__ import annotations

import sys

from app.config import Config
from app.errors import AppError, ConfigConflictError
from app.runtime import build_runtime
from app.server import build_server


def main() -> int:
    config = Config.from_env()
    try:
        runtime = build_runtime(config)
    except AppError as exc:
        # Configuration/fixture files are malformed. Fail fast, before the
        # database is opened, so no half-initialized file can appear.
        print(f"FATAL configuration error: {exc.message}", file=sys.stderr, flush=True)
        for key, value in (exc.details or {}).items():
            print(f"  {key}: {value}", file=sys.stderr, flush=True)
        return 2

    airport_count = (
        len(runtime.airport_config) if runtime.airport_config is not None else 0
    )
    if runtime.conflicting:
        gate_error: ConfigConflictError = runtime.state.gate_error  # type: ignore[assignment]
        print(
            "STARTUP GATE FAILED: airport configuration conflicts with facts "
            "registered in the database. This instance will stay alive for "
            "diagnostics but will NOT serve traffic (/readyz=503).",
            file=sys.stderr,
            flush=True,
        )
        print(f"  {gate_error.message}", file=sys.stderr, flush=True)
        for issue in gate_error.details.get("issues", []):
            print(f"  - {issue}", file=sys.stderr, flush=True)
    else:
        print(
            f"airport-disruption service listening on {config.host}:{config.port} "
            f"(db={config.db_path}, airports={airport_count}, "
            f"gate={runtime.gate_status.state}, "
            f"config_digest={runtime.gate_status.digest})",  # type: ignore[union-attr]
            flush=True,
        )

    server = build_server(config.host, config.port, runtime.state)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        runtime.repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
