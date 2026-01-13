import asyncio
import time

import pytest

from twinops.common.settings import Settings
from twinops.opservice.main import OperationServer


@pytest.mark.asyncio
async def test_opservice_job_cleanup_purges_completed_jobs():
    settings = Settings(
        opservice_job_retention_seconds=0.05,
        opservice_job_cleanup_interval=0.05,
    )
    server = OperationServer(settings)
    await server.startup()
    try:
        result = await server._executor.execute("GetStatus", [], simulate=False)
        job_id = result["jobId"]

        job = None
        for _ in range(50):
            job = server._executor.get_job(job_id)
            if job and job.status in {"COMPLETED", "FAILED"}:
                break
            await asyncio.sleep(0.01)

        assert job is not None
        job.completed_at = time.time() - 1.0

        await asyncio.sleep(0.2)
        assert server._executor.get_job(job_id) is None
    finally:
        await server.shutdown()
