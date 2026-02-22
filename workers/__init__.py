from .process_manager import WorkerProcessManager

_worker_manager: WorkerProcessManager | None = None


def get_worker_manager() -> WorkerProcessManager:
    global _worker_manager
    if _worker_manager is None:
        _worker_manager = WorkerProcessManager()
        _worker_manager.queues = {}
    return _worker_manager
