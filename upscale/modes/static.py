import asyncio, time, torch
from PIL import Image
import comfy.model_management
from ...utils.logging import debug_log, log
from ...utils.image import tensor_to_pil, pil_to_tensor
from ...utils.async_helpers import run_async_in_server_loop
from ...utils.config import get_worker_timeout_seconds
from ...utils.constants import TILE_WAIT_TIMEOUT, TILE_SEND_TIMEOUT, MAX_BATCH
from ...utils.usdu_managment import (
    ensure_tile_jobs_initialized, init_static_job_batched,
    _mark_task_completed, _cleanup_job, _drain_results_queue,
    _send_heartbeat_to_master,
    JOB_PENDING_TASKS, JOB_WORKER_STATUS,
)


class StaticModeMixin:
    def _process_worker_static_sync(self, upscaled_image, model, positive, negative, vae,
                                    seed, steps, cfg, sampler_name, scheduler, denoise,
                                    tile_width, tile_height, padding, mask_blur,
                                    force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                                    worker_id, enabled_workers):
        """Worker static mode processing with optional dynamic queue pulling."""
        # Round tile dimensions
        tile_width = self.round_to_multiple(tile_width)
        tile_height = self.round_to_multiple(tile_height)
        
        # Get dimensions and calculate tiles
        _, height, width, _ = upscaled_image.shape
        all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)
        num_tiles_per_image = len(all_tiles)
        batch_size = upscaled_image.shape[0]
        total_tiles = batch_size * num_tiles_per_image
        
        processed_tiles = []
        sliced_conditioning_cache = {}
        
        # Dynamic queue mode (static processing): process batched-per-tile
        log(f"USDU Dist Worker[{worker_id[:8]}]: Canvas {width}x{height} | Tile {tile_width}x{tile_height} | Tiles/image {num_tiles_per_image} | Batch {batch_size}")
        processed_count = 0

        # Poll for job readiness
        max_poll_attempts = 20
        for attempt in range(max_poll_attempts):
            ready = run_async_in_server_loop(
                self._check_job_status(multi_job_id, master_url),
                timeout=5.0
            )
            if ready:
                debug_log(f"Worker[{worker_id[:8]}] job {multi_job_id} ready after {attempt} attempts")
                break
            time.sleep(1.0)
        else:
            log(f"Job {multi_job_id} not ready after {max_poll_attempts} attempts, aborting")
            return (upscaled_image,)

        # Main processing loop - pull tile ids from queue
        while True:
            # Request a tile to process
            tile_idx, estimated_remaining, batched_static = run_async_in_server_loop(
                self._request_tile_from_master(multi_job_id, master_url, worker_id),
                timeout=TILE_WAIT_TIMEOUT
            )

            if tile_idx is None:
                debug_log(f"Worker[{worker_id[:8]}] - No more tiles to process")
                break

            # Always batched-per-tile in static mode
            debug_log(f"Worker[{worker_id[:8]}] - Assigned tile_id {tile_idx}")
            processed_count += batch_size
            tile_id = tile_idx
            tx, ty = all_tiles[tile_id]
            # Extract tile for entire batch
            tile_batch, x1, y1, ew, eh = self.extract_batch_tile_with_padding(
                upscaled_image, tx, ty, tile_width, tile_height, padding, force_uniform_tiles
            )
            # Process batch
            region = (x1, y1, x1 + ew, y1 + eh)
            processed_batch = self.process_tiles_batch(
                tile_batch, model, positive, negative, vae,
                seed, steps, cfg, sampler_name, scheduler, denoise, tiled_decode,
                region, (width, height)
            )
            # Queue results
            for b in range(batch_size):
                processed_tiles.append({
                    'tile': processed_batch[b:b+1],
                    'tile_idx': tile_id,
                    'x': x1,
                    'y': y1,
                    'extracted_width': ew,
                    'extracted_height': eh,
                    'padding': padding,
                    'batch_idx': b,
                    'global_idx': b * num_tiles_per_image + tile_id
                })

            # Send heartbeat
            try:
                run_async_in_server_loop(
                    _send_heartbeat_to_master(multi_job_id, master_url, worker_id),
                    timeout=5.0
                )
            except Exception as e:
                debug_log(f"Worker[{worker_id[:8]}] heartbeat failed: {e}")

            # Send tiles in batches within loop
            if len(processed_tiles) >= MAX_BATCH:
                run_async_in_server_loop(
                    self.send_tiles_batch_to_master(processed_tiles, multi_job_id, master_url, padding, worker_id),
                    timeout=TILE_SEND_TIMEOUT
                )
                processed_tiles = []

        # Send any remaining tiles
        if processed_tiles:
            run_async_in_server_loop(
                self.send_tiles_batch_to_master(processed_tiles, multi_job_id, master_url, padding, worker_id),
                timeout=TILE_SEND_TIMEOUT
            )
        
        debug_log(f"Worker {worker_id} completed all assigned and requeued tiles")
        return (upscaled_image,)

    async def _async_collect_and_monitor_static(self, multi_job_id, total_tiles, expected_total):
        """Async helper for collection and monitoring in static mode.
        Returns collected tasks dict. Caller should check if all tasks are complete."""
        last_progress_log = time.time()
        progress_interval = 5.0
        last_heartbeat_check = time.time()
        last_completed_count = 0
        
        while True:
            # Check for user interruption
            if comfy.model_management.processing_interrupted():
                log("Processing interrupted by user")
                raise comfy.model_management.InterruptProcessingException()
            
            # Drain any pending results
            collected_count = await _drain_results_queue(multi_job_id)
            
            # Check and requeue timed-out workers periodically
            current_time = time.time()
            if current_time - last_heartbeat_check >= 10.0:
                requeued_count = await self._check_and_requeue_timed_out_workers(multi_job_id, expected_total)
                if requeued_count > 0:
                    log(f"Requeued {requeued_count} tasks from timed-out workers")
                last_heartbeat_check = current_time
            
            # Get current completion count
            completed_count = await _get_completed_count(multi_job_id)
            
            # Progress logging
            if current_time - last_progress_log >= progress_interval:
                log(f"Progress: {completed_count}/{expected_total} tasks completed")
                last_progress_log = current_time
            
            # Check if all tasks are completed
            if completed_count >= expected_total:
                debug_log(f"All {expected_total} tasks completed")
                break
            
            # If no active workers remain and there are pending tasks, return for local processing
            prompt_server = ensure_tile_jobs_initialized()
            async with prompt_server.distributed_tile_jobs_lock:
                job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
                if job_data:
                    pending_queue = job_data.get(JOB_PENDING_TASKS)
                    active_workers = list(job_data.get(JOB_WORKER_STATUS, {}).keys())
                    if pending_queue and not pending_queue.empty() and len(active_workers) == 0:
                        log(f"No active workers remaining with {expected_total - completed_count} tasks pending. Returning for local processing.")
                        break
            
            # Wait a bit before next check
            await asyncio.sleep(0.1)
        
        # Get all completed tasks for return
        return await self._get_all_completed_tasks(multi_job_id)

    def _process_master_static_sync(self, upscaled_image, model, positive, negative, vae,
                                    seed, steps, cfg, sampler_name, scheduler, denoise,
                                    tile_width, tile_height, padding, mask_blur,
                                    force_uniform_tiles, tiled_decode, multi_job_id, enabled_workers,
                                    all_tiles, num_tiles_per_image):
        """Static mode master processing with optional dynamic queue pulling."""
        batch_size = upscaled_image.shape[0]
        _, height, width, _ = upscaled_image.shape
        total_tiles = batch_size * num_tiles_per_image
        
        # Convert batch to PIL list for processing
        result_images = []
        for b in range(batch_size):
            image_pil = tensor_to_pil(upscaled_image[b:b+1], 0)
            result_images.append(image_pil.copy())
        
        sliced_conditioning_cache = {}
        # Initialize queue: pending queue holds tile ids (batched per tile)
        log("USDU Dist: Using tile queue distribution")
        run_async_in_server_loop(
            init_static_job_batched(multi_job_id, batch_size, num_tiles_per_image, enabled_workers),
            timeout=10.0
        )
        debug_log(
            f"Initialized tile-id queue with {num_tiles_per_image} ids for batch {batch_size}"
        )

        # Precompute masks for all tile positions to avoid repeated Gaussian blur work during blending
        tile_masks = []
        for idx, (tx, ty) in enumerate(all_tiles):
            tile_masks.append(self.create_tile_mask(width, height, tx, ty, tile_width, tile_height, mask_blur))

        processed_count = 0
        consecutive_no_tile = 0
        max_consecutive_no_tile = 2

        while processed_count < total_tiles:
            comfy.model_management.throw_exception_if_processing_interrupted()
            tile_idx = run_async_in_server_loop(
                self._get_next_tile_index(multi_job_id),
                timeout=5.0
            )
            if tile_idx is not None:
                consecutive_no_tile = 0
                processed_count += batch_size
                tile_id = tile_idx
                tx, ty = all_tiles[tile_id]
                tile_batch, x1, y1, ew, eh = self.extract_batch_tile_with_padding(
                    upscaled_image, tx, ty, tile_width, tile_height, padding, force_uniform_tiles
                )
                region = (x1, y1, x1 + ew, y1 + eh)
                processed_batch = self.process_tiles_batch(
                    tile_batch, model, positive, negative, vae,
                    seed, steps, cfg, sampler_name, scheduler, denoise, tiled_decode,
                    region, (width, height)
                )
                tile_mask = tile_masks[tile_id]
                for b in range(batch_size):
                    tile_pil = tensor_to_pil(processed_batch, b)
                    if tile_pil.size != (ew, eh):
                        tile_pil = tile_pil.resize((ew, eh), Image.LANCZOS)
                    result_images[b] = self.blend_tile(result_images[b], tile_pil, x1, y1, (ew, eh), tile_mask, padding)
                    global_idx = b * num_tiles_per_image + tile_id
                    run_async_in_server_loop(
                        _mark_task_completed(multi_job_id, global_idx, {'batch_idx': b, 'tile_idx': tile_id}),
                        timeout=5.0
                    )
                log(f"USDU Dist: Tiles progress {processed_count}/{total_tiles} (tile {tile_id})")
            else:
                consecutive_no_tile += 1
                if consecutive_no_tile >= max_consecutive_no_tile:
                    debug_log(f"Master processed {processed_count} tiles, moving to collection phase")
                    break
                time.sleep(0.1)
        master_processed_count = processed_count
        
        # Continue processing any remaining tiles while collecting worker results
        remaining_tiles = total_tiles - master_processed_count
        if remaining_tiles > 0:
            debug_log(f"Master waiting for {remaining_tiles} tiles from workers")
            
            # Collect worker results using async operations
            try:
                # Wait until either all tasks are collected or there are no active workers left
                collected_tasks = run_async_in_server_loop(
                    self._async_collect_and_monitor_static(multi_job_id, total_tiles, expected_total=total_tiles),
                    timeout=None
                )
            except comfy.model_management.InterruptProcessingException:
                # Clean up job on interruption
                run_async_in_server_loop(_cleanup_job(multi_job_id), timeout=5.0)
                raise
            
            # Check if we need to process any remaining tasks locally after collection
            completed_count = len(collected_tasks)
            if completed_count < total_tiles:
                log(f"Processing remaining {total_tiles - completed_count} tasks locally after worker failures")
                
                # Process any remaining pending tasks (batched-per-tile)
                while True:
                    # Check for user interruption
                    comfy.model_management.throw_exception_if_processing_interrupted()

                    # Get next tile_id from pending queue
                    tile_id = run_async_in_server_loop(
                        self._get_next_tile_index(multi_job_id),
                        timeout=5.0
                    )

                    if tile_id is None:
                        break

                    # Extract batched tile and process across available batch
                    tx, ty = all_tiles[tile_id]
                    tile_batch, x1, y1, ew, eh = self.extract_batch_tile_with_padding(
                        upscaled_image, tx, ty, tile_width, tile_height, padding, force_uniform_tiles
                    )
                    region = (x1, y1, x1 + ew, y1 + eh)
                    processed_batch = self.process_tiles_batch(
                        tile_batch, model, positive, negative, vae,
                        seed, steps, cfg, sampler_name, scheduler, denoise, tiled_decode,
                        region, (width, height)
                    )
                    tile_mask = tile_masks[tile_id]
                    out_bs = processed_batch.shape[0] if hasattr(processed_batch, 'shape') else batch_size
                    for b in range(min(batch_size, out_bs)):
                        tile_pil = tensor_to_pil(processed_batch, b)
                        if tile_pil.size != (ew, eh):
                            tile_pil = tile_pil.resize((ew, eh), Image.LANCZOS)
                        result_images[b] = self.blend_tile(result_images[b], tile_pil, x1, y1, (ew, eh), tile_mask, padding)
                        global_idx = b * num_tiles_per_image + tile_id
                        # Mark as completed so the collector state is consistent
                        run_async_in_server_loop(
                            _mark_task_completed(multi_job_id, global_idx, {'batch_idx': b, 'tile_idx': tile_id}),
                            timeout=5.0
                        )
        else:
            # Master processed all tiles
            collected_tasks = run_async_in_server_loop(
                self._get_all_completed_tasks(multi_job_id),
                timeout=5.0
            )
        
        # Blend worker tiles synchronously
        for global_idx, tile_data in collected_tasks.items():
            # Skip tiles that don't have tensor data (already processed)
            if 'tensor' not in tile_data and 'image' not in tile_data:
                continue
            
            batch_idx = tile_data.get('batch_idx', global_idx // num_tiles_per_image)
            tile_idx = tile_data.get('tile_idx', global_idx % num_tiles_per_image)
            
            if batch_idx >= batch_size:
                continue
            
            # Blend tile synchronously
            x = tile_data.get('x', 0)
            y = tile_data.get('y', 0)
            # Prefer PIL image if present to avoid reconversion
            if 'image' in tile_data:
                tile_pil = tile_data['image']
            else:
                tile_tensor = tile_data['tensor']
                tile_pil = tensor_to_pil(tile_tensor, 0)
            orig_x, orig_y = all_tiles[tile_idx]
            tile_mask = tile_masks[tile_idx]
            extracted_width = tile_data.get('extracted_width', tile_width + 2 * padding)
            extracted_height = tile_data.get('extracted_height', tile_height + 2 * padding)
            result_images[batch_idx] = self.blend_tile(result_images[batch_idx], tile_pil,
                                                      x, y, (extracted_width, extracted_height), tile_mask, padding)
        
        try:
            # Convert back to tensor
            if batch_size == 1:
                result_tensor = pil_to_tensor(result_images[0])
            else:
                result_tensors = [pil_to_tensor(img) for img in result_images]
                result_tensor = torch.cat(result_tensors, dim=0)
            
            if upscaled_image.is_cuda:
                result_tensor = result_tensor.cuda()
            
            log(f"UltimateSDUpscale Master - Job {multi_job_id} complete")
            return (result_tensor,)
        finally:
            # Cleanup (async operation) - always execute
            run_async_in_server_loop(_cleanup_job(multi_job_id), timeout=5.0)
