from dataclasses import dataclass, field
import asyncio
import time


class BaseJobState:
    """Marker base class for typed USDU job state containers."""


@dataclass
class TileJobState(BaseJobState):
    """Typed state container for static (tile) USDU jobs."""

    multi_job_id: str
    mode: str = field(default="static", init=False)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending_tasks: asyncio.Queue = field(default_factory=asyncio.Queue)
    completed_tasks: dict = field(default_factory=dict)
    worker_status: dict = field(default_factory=dict)
    assigned_to_workers: dict = field(default_factory=dict)
    batch_size: int = 0
    num_tiles_per_image: int = 0
    batched_static: bool = False
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class ImageJobState(BaseJobState):
    """Typed state container for dynamic (per-image) USDU jobs."""

    multi_job_id: str
    mode: str = field(default="dynamic", init=False)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending_images: asyncio.Queue = field(default_factory=asyncio.Queue)
    completed_images: dict = field(default_factory=dict)
    worker_status: dict = field(default_factory=dict)
    assigned_to_workers: dict = field(default_factory=dict)
    batch_size: int = 0
    num_tiles_per_image: int = 0
    batched_static: bool = False
    created_at: float = field(default_factory=time.monotonic)

    @property
    def pending_tasks(self):
        return self.pending_images

    @property
    def completed_tasks(self):
        return self.completed_images
