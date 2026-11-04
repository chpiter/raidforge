from __future__ import annotations

import threading

from webplane.config import Settings
from webplane.db import connect, init_db
from webplane.services import agents as agent_service
from webplane.services import tasks as task_service
from webplane.timeutil import utcnow


def test_concurrent_claim_has_no_duplicates(tmp_path):
    settings = Settings(data_dir=tmp_path, maintenance=False, agent_token="")
    init_db(settings)
    conn = connect(settings.db_path)
    try:
        agent_service.upsert_heartbeat(
            conn,
            {"agent_id": "agent-1", "server_sn": "SN-1", "agent_version": "1.2.0"},
            "127.0.0.1",
            utcnow(),
            offline_seconds=45,
        )
        agent_row = agent_service.get_agent(conn, "agent-1")
        for _ in range(20):
            task_service.create_task(
                conn, settings, agent_row, "list_vds", {}, now=utcnow()
            )
    finally:
        conn.close()

    claimed = []
    lock = threading.Lock()

    def worker():
        local = connect(settings.db_path)
        try:
            while True:
                task = task_service.claim_next_task(local, settings, "agent-1", "SN-1")
                if task is None:
                    break
                with lock:
                    claimed.append(task["task_id"])
        finally:
            local.close()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(claimed) == 20
    assert len(set(claimed)) == 20

    verify = connect(settings.db_path)
    try:
        row = verify.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='leased'").fetchone()
    finally:
        verify.close()
    assert row["n"] == 20
