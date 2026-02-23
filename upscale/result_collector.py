import asyncio, time
import comfy.model_management
import server
from ..utils.logging import debug_log, log
from ..utils.config import get_worker_timeout_seconds
from ..utils.usdu_managment import (
    ensure_tile_jobs_initialized, _mark_task_completed,
    _check_and_requeue_timed_out_workers,
    JOB_COMPLETED_TASKS, JOB_WORKER_STATUS, JOB_PENDING_TASKS,
)


class ResultCollectorMixin:
    async def _async_collect_results(self, multi_job_id, num_workers, mode='static', 
                                   remaining_to_collect=None, batch_size=None):
        """Unified async helper to collect results from workers (tiles or images)."""
        # Get the already initialized queue
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id not in prompt_server.distributed_pending_tile_jobs:
                raise RuntimeError(f"Job queue not initialized for {multi_job_id}")
            job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
            if not isinstance(job_data, dict) or 'mode' not in job_data:
                raise RuntimeError("Invalid job data structure")
            if job_data['mode'] != mode:
                raise RuntimeError(f"Mode mismatch: expected {mode}, got {job_data['mode']}")
            q = job_data['queue']
            
            # For dynamic mode, get reference to completed_images
            if mode == 'dynamic':
                completed_images = job_data['completed_images']
                # Calculate expected count for logging
                expected_count = remaining_to_collect or batch_size
            else:
                # Calculate expected count from job data for static mode
                expected_count = job_data.get('total_items', 0) - len(job_data.get('completed_items', set()))
        
        item_type = "images" if mode == 'dynamic' else "tiles"
        debug_log(f"UltimateSDUpscale Master - Starting collection, expecting {expected_count} {item_type} from {num_workers} workers")
        
        collected_results = {}
        workers_done = set()
        # Unify collector/upscaler wait behavior with the UI worker timeout
        timeout = float(get_worker_timeout_seconds())
        last_heartbeat_check = time.time()
        collected_count = 0
        
        while len(workers_done) < num_workers:
            # Check for user interruption
            if comfy.model_management.processing_interrupted():
                log("Processing interrupted by user")
                raise comfy.model_management.InterruptProcessingException()
                
            # For dynamic mode with remaining_to_collect, check if we've collected enough
            if mode == 'dynamic' and remaining_to_collect and collected_count >= remaining_to_collect:
                break
                
            try:
                # Shorter poll for dynamic mode, but never exceed the configured timeout
                wait_timeout = (min(10.0, timeout) if mode == 'dynamic' else timeout)
                result = await asyncio.wait_for(q.get(), timeout=wait_timeout)
                worker_id = result['worker_id']
                is_last = result.get('is_last', False)
                
                if mode == 'static':
                    # Handle tiles
                    tiles = result.get('tiles', [])
                    if tiles:
                        # Batch mode
                        debug_log(f"UltimateSDUpscale Master - Received batch of {len(tiles)} tiles from worker '{worker_id}' (is_last={is_last})")
                        
                        for tile_data in tiles:
                            # Validate required fields
                            if 'batch_idx' not in tile_data:
                                log(f"UltimateSDUpscale Master - Missing batch_idx in tile data, skipping")
                                continue
                            
                            tile_idx = tile_data['tile_idx']
                            # Use global_idx as key if available (for batch processing)
                            key = tile_data.get('global_idx', tile_idx)
                            
                            # Store the full tile data including metadata; prefer PIL image if present
                            entry = {
                                'tile_idx': tile_idx,
                                'x': tile_data['x'],
                                'y': tile_data['y'],
                                'extracted_width': tile_data['extracted_width'],
                                'extracted_height': tile_data['extracted_height'],
                                'padding': tile_data['padding'],
                                'worker_id': worker_id,
                                'batch_idx': tile_data.get('batch_idx', 0),
                                'global_idx': tile_data.get('global_idx', tile_idx)
                            }
                            if 'image' in tile_data:
                                entry['image'] = tile_data['image']
                            elif 'tensor' in tile_data:
                                entry['tensor'] = tile_data['tensor']
                            collected_results[key] = entry
                    else:
                        # Single tile mode (backward compat)
                        tile_idx = result['tile_idx']
                        collected_results[tile_idx] = result
                        debug_log(f"UltimateSDUpscale Master - Received single tile {tile_idx} from worker '{worker_id}' (is_last={is_last})")
                
                elif mode == 'dynamic':
                    # Handle full images
                    if 'image_idx' in result and 'image' in result:
                        image_idx = result['image_idx']
                        image_pil = result['image']
                        completed_images[image_idx] = image_pil
                        collected_results[image_idx] = image_pil
                        collected_count += 1
                        debug_log(f"UltimateSDUpscale Master - Received image {image_idx} from worker {worker_id}")
                
                if is_last:
                    workers_done.add(worker_id)
                    debug_log(f"UltimateSDUpscale Master - Worker {worker_id} completed")
                    
            except asyncio.TimeoutError:
                if mode == 'dynamic':
                    # Check for worker timeouts periodically
                    current_time = time.time()
                    if current_time - last_heartbeat_check >= 10.0:
                        # Use the class method to check and requeue
                        requeued = await self._check_and_requeue_timed_out_workers(multi_job_id, batch_size)
                        if requeued > 0:
                            log(f"UltimateSDUpscale Master - Requeued {requeued} images from timed out workers")
                        last_heartbeat_check = current_time
                    
                    # Check if we've been waiting too long overall
                    if current_time - last_heartbeat_check > timeout:
                        log(f"UltimateSDUpscale Master - Overall timeout waiting for images")
                        break
                else:
                    log(f"UltimateSDUpscale Master - Timeout waiting for {item_type}")
                    break
        
        debug_log(f"UltimateSDUpscale Master - Collection complete. Got {len(collected_results)} {item_type} from {len(workers_done)} workers")
        
        # Clean up job queue
        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                del prompt_server.distributed_pending_tile_jobs[multi_job_id]
        
        return collected_results if mode == 'static' else completed_images

    async def _async_collect_worker_tiles(self, multi_job_id, num_workers):
        """Async helper to collect tiles from workers."""
        return await self._async_collect_results(multi_job_id, num_workers, mode='static')

    async def _mark_image_completed(self, multi_job_id, image_idx, image_pil):
        """Mark an image as completed in the job data."""
        # Mark the image as completed with the image data
        await _mark_task_completed(multi_job_id, image_idx, {'image': image_pil})
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if job_data and 'completed_images' in job_data:
                job_data['completed_images'][image_idx] = image_pil

    async def _async_collect_dynamic_images(self, multi_job_id, remaining_to_collect, num_workers, batch_size, master_processed_count):
        """Collect remaining processed images from workers."""
        return await self._async_collect_results(multi_job_id, num_workers, mode='dynamic', 
                                               remaining_to_collect=remaining_to_collect, 
                                               batch_size=batch_size)
