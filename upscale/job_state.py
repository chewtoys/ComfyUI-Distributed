import asyncio, time
import server
from ..utils.logging import debug_log
from ..utils.usdu_managment import (
    ensure_tile_jobs_initialized,
    _check_and_requeue_timed_out_workers as _requeue_usdu,
    JOB_COMPLETED_TASKS, JOB_PENDING_TASKS,
)


class JobStateMixin:
    async def _get_all_completed_tasks(self, multi_job_id):
        """Helper to retrieve all completed tasks from the job data."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if job_data and JOB_COMPLETED_TASKS in job_data:
                return dict(job_data[JOB_COMPLETED_TASKS])  # Return a copy
            return {}

    async def _get_next_image_index(self, multi_job_id):
        """Get next image index from pending queue for master."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if not job_data or 'pending_images' not in job_data:
                return None
            try:
                image_idx = await asyncio.wait_for(job_data['pending_images'].get(), timeout=1.0)
                return image_idx
            except asyncio.TimeoutError:
                return None

    async def _get_next_tile_index(self, multi_job_id):
        """Get next tile index from pending queue for master in static mode."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if not job_data or JOB_PENDING_TASKS not in job_data:
                return None
            try:
                tile_idx = await asyncio.wait_for(job_data[JOB_PENDING_TASKS].get(), timeout=0.1)
                return tile_idx
            except asyncio.TimeoutError:
                return None

    async def _get_total_completed_count(self, multi_job_id):
        """Get total count of all completed images (master + workers)."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if job_data and 'completed_images' in job_data:
                return len(job_data['completed_images'])
            return 0

    async def _get_all_completed_images(self, multi_job_id):
        """Get all completed images."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if job_data and 'completed_images' in job_data:
                return job_data['completed_images'].copy()
            return {}

    async def _get_pending_count(self, multi_job_id):
        """Get count of pending images in the queue."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if job_data and 'pending_images' in job_data:
                return job_data['pending_images'].qsize()
            return 0

    async def _drain_worker_results_queue(self, multi_job_id):
        """Drain pending worker results from queue and update completed_images. Returns count of drained images."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if not job_data or 'queue' not in job_data or 'completed_images' not in job_data:
                return 0
            q = job_data['queue']
            completed_images = job_data['completed_images']

            collected = 0
            while not q.empty():
                try:
                    result = await asyncio.wait_for(q.get(), timeout=0.1)
                    worker_id = result['worker_id']
                    is_last = result.get('is_last', False)

                    if 'image_idx' in result and 'image' in result:
                        image_idx = result['image_idx']
                        image_pil = result['image']
                        if image_idx not in completed_images:
                            completed_images[image_idx] = image_pil
                            collected += 1
                            debug_log(f"Drained image {image_idx} from worker {worker_id}")

                    if is_last:
                        # Optional: track worker completion if needed
                        pass
                except asyncio.TimeoutError:
                    break  # No more immediately available

            if collected > 0:
                debug_log(f"Drained {collected} worker images during retry")
            
            return collected

    async def _check_and_requeue_timed_out_workers(self, multi_job_id, batch_size):
        """Check for timed out workers and requeue their assigned images. Returns count of requeued images."""
        # Use the original function from usdu_managment.py
        return await _requeue_usdu(multi_job_id, batch_size)
