from datetime import datetime, timezone
from typing import Callable, List


class Scheduler:
    def __init__(self) -> None:
        self._jobs: List[tuple[datetime, Callable[[], None], str]] = []

    def add_job(self, run_at: datetime, callback: Callable[[], None], name: str) -> None:
        self._jobs.append((run_at, callback, name))

    def run_due_jobs(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        completed = []
        remaining = []
        for run_at, callback, name in self._jobs:
            if run_at <= now:
                callback()
                completed.append({"name": name, "status": "executed", "run_at": run_at.isoformat()})
            else:
                remaining.append((run_at, callback, name))
        self._jobs = remaining
        return completed


scheduler = Scheduler()
