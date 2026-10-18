"""有界容量模式：原 Owner 的采样与合成延迟，不产生业务执行事实。"""
import asyncio
import contextvars
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
import threading
import time

import psutil
from sqlalchemy import text
from core.persistence.database import Database, DatabaseConfig

_ingress = contextvars.ContextVar("capacity_ingress", default=None)


async def capacity_database(url):
    admin = Database(DatabaseConfig(url=url, pool_size=1, max_overflow=0))
    async with admin.transaction() as session:
        role = (await session.execute(text("SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname='capacity_core'"))).first()
        if role is None:
            await session.execute(text("CREATE ROLE capacity_core LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"))
        else:
            assert not any(role), "capacity role privilege mismatch"
        await session.execute(text("GRANT USAGE ON SCHEMA public TO capacity_core"))
        await session.execute(text("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO capacity_core"))
        await session.execute(text("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO capacity_core"))
    await admin.dispose()
    # 仅此隔离 loopback 测试 URL；不输出凭据，不改生产连接。
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.port == 55432 and parsed.database.endswith("_test")
    result = Database(DatabaseConfig(url=parsed.set(username="capacity_core", password=None).render_as_string(hide_password=False),
        use_null_pool=False, pool_size=5, max_overflow=5))
    return result


class CapacityProbe:
    def __init__(self, directory, database):
        self.directory, self.database = directory, database
        self.rows, self.counts = [], {"roots": 0, "models": 0, "tools": 0}
        self.lock = threading.Lock()
        self.started = time.perf_counter()
        self.go_process = next((p for p in psutil.Process().parents() if p.name().lower() == "postgres.test.exe"), None)
        if self.go_process: self.go_process.cpu_percent()
        self.factory = None
        self.stop_reason = None

    def record(self, kind, started, **values):
        with self.lock:
            self.rows.append({"kind": kind, "ms": (time.perf_counter()-started)*1000,
                "utc": datetime.now(UTC).isoformat(), **values})

    def count(self, kind, maximum):
        with self.lock:
            self.counts[kind] += 1
            if self.counts[kind] > maximum or self.stop_reason:
                raise RuntimeError("CAPACITY_HARD_STOP")

    def enter_request(self):
        _ingress.set(time.perf_counter())

    def install(self):
        from core.persistence.repositories.agent_quality import AgentQualityReceiptRepository
        original_admit, original_capture = AgentQualityReceiptRepository.admit, AgentQualityReceiptRepository.capture
        probe = self
        async def admit(repo, *args, **kwargs):
            started = _ingress.get() or time.perf_counter()
            result = await original_admit(repo, *args, **kwargs)
            if result[0]: probe.count("roots", 750)
            probe.record("admission", started, winner=result[0], run_id=result[1].remote_run_id)
            return result
        async def capture(repo, *args, **kwargs):
            started = time.perf_counter()
            result = await original_capture(repo, *args, **kwargs)
            probe.record("seal", started, bytes=len(result.encode()) if result else 0)
            return result
        AgentQualityReceiptRepository.admit, AgentQualityReceiptRepository.capture = admit, capture

    def provider(self, provider):
        original = provider.invoke_request
        def invoke(*args, **kwargs):
            self.count("models", 6000)
            started = time.perf_counter()
            time.sleep(.05)
            result = original(*args, **kwargs)
            self.record("model_io", started, run_id=kwargs["run_context"].run_id)
            return result
        provider.invoke_request = invoke
        return provider

    def tool(self, tool):
        original = tool.invoke_once
        def invoke(*args, **kwargs):
            self.count("tools", 3000)
            started = time.perf_counter()
            time.sleep(.02)
            result = original(*args, **kwargs)
            self.record("tool_io", started)
            return result
        tool.invoke_once = invoke
        return tool

    def attach(self, factory):
        from core.runtime.metrics import InMemoryMetricsRecorder
        from core.runtime.tracing import InMemorySpanRecorder
        self.metrics, self.spans = InMemoryMetricsRecorder(), InMemorySpanRecorder()
        factory._services = replace(factory.services, runtime_metrics_recorder=self.metrics, span_recorder=self.spans)
        self.factory = factory

    def save(self):
        (self.directory / "capacity-metrics.json").write_text(json.dumps({"counts": self.counts,
            "stop_reason": self.stop_reason, "rows": self.rows}), encoding="utf-8")

    async def sample(self):
        process = psutil.Process()
        process.cpu_percent()
        ticks = 0
        with (self.directory / "capacity-resources.jsonl").open("a", encoding="utf-8") as stream:
            while True:
                start = time.perf_counter()
                memory = process.memory_info()
                sample = {"utc": datetime.now(UTC).isoformat(), "elapsed": start-self.started,
                    "pid": os.getpid(), "rss": memory.rss, "private": getattr(memory,"private",None),
                    "cpu_one_core_percent": process.cpu_percent(), "threads": process.num_threads(),
                    "tasks": len(asyncio.all_tasks()), "counts": dict(self.counts),
                    "pool_size": self.database.engine.pool.size(), "checked_out": self.database.engine.pool.checkedout(),
                    "overflow": self.database.engine.pool.overflow(), "pool_class": type(self.database.engine.pool).__name__}
                if self.go_process:
                    sample.update(go_rss=self.go_process.memory_info().rss, go_threads=self.go_process.num_threads(),
                        go_cpu_one_core_percent=self.go_process.cpu_percent())
                if self.factory:
                    sample["active_runs"] = len(self.factory.services.run_registry.active_handles())
                    sample["histogram_samples"] = sum(len(v) for v in self.metrics._histograms.values())
                    sample["span_records"] = len(self.spans._records)
                if ticks % 5 == 0:
                    try:
                        async with self.database.session() as session:
                            row = (await session.execute(text("SELECT current_user, (SELECT count(*) FROM pg_stat_activity), (SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock'), pg_database_size(current_database())"))).one()
                            sample.update(db_role=row[0], pg_connections=row[1], pg_lock_waiters=row[2], db_bytes=row[3])
                    except Exception as exc:
                        sample["pg_error"] = type(exc).__name__
                if memory.rss > 2*1024**3 or sample.get("go_rss", 0) > 1024**3 or psutil.virtual_memory().available < 6*1024**3 or psutil.disk_usage(str(self.directory)).free < 10*1024**3:
                    self.stop_reason = "RESOURCE_STOP"
                    if self.factory: self.factory.services.admission_gate.close_admission()
                stream.write(json.dumps(sample)+"\n"); stream.flush()
                self.save()
                ticks += 1
                await asyncio.sleep(max(.01, 1-(time.perf_counter()-start)))
