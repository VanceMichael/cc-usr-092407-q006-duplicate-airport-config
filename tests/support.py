"""测试共享辅助函数。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import ROOT, airports_digest, load_airports, load_flights
from app.repository import Repository
from app.service import DisruptionService

FIXTURES_DIR = ROOT / "fixtures"


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.airports = load_airports(FIXTURES_DIR)
        self.flights = load_flights(FIXTURES_DIR, self.airports)
        self.config_digest = airports_digest(self.airports)
        self.repo = Repository(self.db_path)
        # 与真实启动路径相同：先登记机场事实，再装配服务。
        self.repo.register_airport_facts(self.airports, self.config_digest)
        self.service = DisruptionService(
            self.repo,
            self.airports,
            self.flights,
            config_digest=self.config_digest,
        )

    def tearDown(self) -> None:
        self.repo.close()
        self._tmp.cleanup()

    def restart_service(self) -> DisruptionService:
        """模拟容器重启后重新打开同一数据库文件。"""
        self.repo.close()
        self.repo = Repository(self.db_path)
        # 重启必须重新通过启动门禁；相同配置核对通过、读取相同摘要。
        self.repo.register_airport_facts(self.airports, self.config_digest)
        self.service = DisruptionService(
            self.repo,
            self.airports,
            self.flights,
            config_digest=self.config_digest,
        )
        return self.service


def base_event(**overrides) -> dict:
    payload = {
        "event_id": "evt-close0000001",
        "event_version": 1,
        "event_type": "airport.closed",
        "airport_code": "APS",
        "effective_from": "2026-09-07T15:00:00Z",
        "effective_until": "2026-09-07T19:00:00Z",
        "reported_at": "2026-09-07T14:00:00Z",
        "reason": "volcanic ash",
    }
    payload.update(overrides)
    return payload
