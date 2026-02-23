"""Custom exceptions for ComfyUI-Distributed."""


class DistributedError(Exception):
    """Base exception for all ComfyUI-Distributed errors."""


class WorkerError(DistributedError):
    """Error related to a specific distributed worker."""

    def __init__(self, message, worker_id=None):
        super().__init__(message)
        self.worker_id = worker_id


class WorkerTimeoutError(WorkerError):
    """Worker did not respond within the expected timeout."""


class WorkerNotAvailableError(WorkerError):
    """Worker is unreachable or not running."""


class JobQueueError(DistributedError):
    """Error in distributed job queue management."""


class TileCollectionError(DistributedError):
    """Error collecting processed tiles from workers."""


class ProcessError(DistributedError):
    """Error managing a worker subprocess."""

    def __init__(self, message, pid=None, worker_id=None):
        super().__init__(message)
        self.pid = pid
        self.worker_id = worker_id


class TunnelError(DistributedError):
    """Error managing the Cloudflare tunnel."""
