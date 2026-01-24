# Import statements (original + new)
import torch
from PIL import Image, ImageFilter, ImageDraw
import json
import asyncio
import aiohttp
import io
import math
import time
from typing import List, Tuple
from functools import wraps

# Import ComfyUI modules
import comfy.samplers
import comfy.model_management

# Import shared utilities
from .utils.logging import debug_log, log
from .utils.image import tensor_to_pil, pil_to_tensor
from .utils.network import get_client_session
from .utils.async_helpers import run_async_in_server_loop
from .utils.config import get_worker_timeout_seconds
from .utils.constants import (
    TILE_COLLECTION_TIMEOUT, TILE_WAIT_TIMEOUT,
    TILE_SEND_TIMEOUT, MAX_BATCH
)

# Import for controller support
from .utils.usdu_utils import (
    crop_cond,
    get_crop_region,
    expand_crop,
)
from .utils.usdu_managment import (
    clone_conditioning, ensure_tile_jobs_initialized,
    # Job management functions
    _drain_results_queue,
    _check_and_requeue_timed_out_workers, _get_completed_count, _mark_task_completed,
    _send_heartbeat_to_master, _cleanup_job,
    # Constants
    JOB_COMPLETED_TASKS, JOB_WORKER_STATUS, JOB_PENDING_TASKS,
    MAX_PAYLOAD_SIZE
)
from .utils.usdu_managment import init_dynamic_job, init_static_job_batched


# Note: MAX_BATCH and HEARTBEAT_TIMEOUT are imported from utils.constants
# They can be overridden via environment variables:
# - COMFYUI_MAX_BATCH (default: 20)
# - COMFYUI_HEARTBEAT_TIMEOUT (default: 90)

# Sync wrapper decorator for async methods
def sync_wrapper(async_func):
    """Decorator to wrap async methods for synchronous execution."""
    @wraps(async_func)
    def sync_func(self, *args, **kwargs):
        # Use run_async_in_server_loop for ComfyUI compatibility
        return run_async_in_server_loop(
            async_func(self, *args, **kwargs),
            timeout=600.0  # 10 minute timeout for long operations
        )
    return sync_func

# Note: tensor_to_pil and pil_to_tensor are imported from utils.image


def _parse_enabled_worker_ids(enabled_worker_ids):
    """Parse enabled worker IDs from either JSON or list input."""
    if isinstance(enabled_worker_ids, list):
        return [str(worker_id) for worker_id in enabled_worker_ids]
    if not enabled_worker_ids:
        return []
    if isinstance(enabled_worker_ids, str):
        try:
            parsed = json.loads(enabled_worker_ids)
        except json.JSONDecodeError:
            log("USDU Dist: Invalid enabled_worker_ids JSON; defaulting to no workers.")
            return []
        if isinstance(parsed, list):
            return [str(wid) for wid in parsed]
    return []


class UltimateSDUpscaleDistributed:
    """
    Distributed version of Ultimate SD Upscale (No Upscale).
    
    Supports three processing modes:
    1. Single GPU: No workers available, process everything locally
    2. Static Mode: Small batches, distributes tiles across workers (flattened)
    3. Dynamic Mode: Large batches, assigns whole images to workers dynamically
    
    Features:
    - Multi-mode batch handling for efficient video/image upscaling
    - Tiled VAE support for memory efficiency
    - Dynamic load balancing for large batches
    - Backward compatible with single-image workflows
    
    Environment Variables:
    - COMFYUI_MAX_BATCH: Chunk size for tile sending (default 20)
    - COMFYUI_MAX_PAYLOAD_SIZE: Max API payload bytes (default 50MB)
    
    Threshold: dynamic_threshold input controls mode switch (default 8)
    """

    def __init__(self):
        """Initialize the node and ensure persistent storage exists."""
        # Pre-initialize the persistent storage on node creation
        ensure_tile_jobs_initialized()
        debug_log("UltimateSDUpscaleDistributed - Node initialized")

    # WAN/FLOW detection removed per user request; enforcing 4n+1 for any batch > 1.
    
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "upscaled_image": ("IMAGE",),
                "model": ("MODEL",),
                "positive": ("CONDITIONING",),
                "negative": ("CONDITIONING",),
                "vae": ("VAE",),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS,),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS,),
                "denoise": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01}),
                "tile_width": ("INT", {"default": 512, "min": 64, "max": 2048, "step": 8}),
                "tile_height": ("INT", {"default": 512, "min": 64, "max": 2048, "step": 8}),
                "padding": ("INT", {"default": 32, "min": 0, "max": 256, "step": 8}),
                "mask_blur": ("INT", {"default": 8, "min": 0, "max": 256}),
                "force_uniform_tiles": ("BOOLEAN", {"default": True}),
                "tiled_decode": ("BOOLEAN", {"default": False}),
            },
            "hidden": {
                "multi_job_id": ("STRING", {"default": ""}),
                "is_worker": ("BOOLEAN", {"default": False}),
                "master_url": ("STRING", {"default": ""}),
                "enabled_worker_ids": ("STRING", {"default": "[]"}),
                "worker_id": ("STRING", {"default": ""}),
                "tile_indices": ("STRING", {"default": ""}),  # Unused - kept for compatibility
                "dynamic_threshold": ("INT", {"default": 8, "min": 1, "max": 64}),
            },
        }
    
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "image/upscaling"
    
    @classmethod
    def IS_CHANGED(cls, **kwargs):
        """Force re-execution."""
        return float("nan")  # Always re-execute
    
    
    def run(self, upscaled_image, model, positive, negative, vae, seed, steps, cfg, 
            sampler_name, scheduler, denoise, tile_width, tile_height, padding, 
            mask_blur, force_uniform_tiles, tiled_decode,
            multi_job_id="", is_worker=False, master_url="", enabled_worker_ids="[]", 
            worker_id="", tile_indices="", dynamic_threshold=8):
        """Entry point - runs SYNCHRONOUSLY like Ultimate SD Upscaler."""
        # Strict WAN/FLOW batching: error if batch is not 4n+1 (except allow 1)
        try:
            batch_size = int(getattr(upscaled_image, 'shape', [1])[0])
        except Exception:
            batch_size = 1
        # Enforce 4n+1 batches globally for any model when batch > 1 (master only)
        if not is_worker and batch_size != 1 and (batch_size % 4 != 1):
            raise ValueError(
                f"Batch size {batch_size} is not of the form 4n+1. "
                "This node requires batch sizes of 1 or 4n+1 (1, 5, 9, 13, ...). "
                "Please adjust the batch size."
            )
        if not multi_job_id:
            # No distributed processing, run single GPU version
            return self.process_single_gpu(upscaled_image, model, positive, negative, vae,
                                          seed, steps, cfg, sampler_name, scheduler, denoise,
                                          tile_width, tile_height, padding, mask_blur, force_uniform_tiles, tiled_decode)
        
        if is_worker:
            # Worker mode: process tiles synchronously
            return self.process_worker(upscaled_image, model, positive, negative, vae,
                                      seed, steps, cfg, sampler_name, scheduler, denoise,
                                      tile_width, tile_height, padding, mask_blur,
                                      force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                                      worker_id, enabled_worker_ids, dynamic_threshold)
        else:
            # Master mode: distribute and collect synchronously
            return self.process_master(upscaled_image, model, positive, negative, vae,
                                     seed, steps, cfg, sampler_name, scheduler, denoise,
                                     tile_width, tile_height, padding, mask_blur,
                                     force_uniform_tiles, tiled_decode, multi_job_id, enabled_worker_ids, 
                                     dynamic_threshold)
    
    def process_worker(self, upscaled_image, model, positive, negative, vae,
                      seed, steps, cfg, sampler_name, scheduler, denoise,
                      tile_width, tile_height, padding, mask_blur,
                      force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                      worker_id, enabled_worker_ids, dynamic_threshold):
        """Unified worker processing - handles both static and dynamic modes."""
        # Get batch size to determine mode
        batch_size = upscaled_image.shape[0]
        
        # Ensure mode consistency across master/workers via shared threshold
        # Determine mode (must match master's logic)
        enabled_workers = json.loads(enabled_worker_ids)
        num_workers = len(enabled_workers)
        # Compute number of tiles for this image to decide if tile distribution makes sense
        _, height, width, _ = upscaled_image.shape
        all_tiles = self.calculate_tiles(width, height, self.round_to_multiple(tile_width), self.round_to_multiple(tile_height), force_uniform_tiles)
        num_tiles_per_image = len(all_tiles)

        mode = self._determine_processing_mode(batch_size, num_workers, dynamic_threshold)
        # For USDU-style processing, we want tile distribution whenever workers are available
        # and there is more than one tile to process, even if batch == 1.
        if num_workers > 0 and num_tiles_per_image > 1:
            mode = "static"
            
        debug_log(f"USDU Dist Worker - Batch size {batch_size}")
        
        if mode == "dynamic":
            return self.process_worker_dynamic(upscaled_image, model, positive, negative, vae,
                                             seed, steps, cfg, sampler_name, scheduler, denoise,
                                             tile_width, tile_height, padding, mask_blur,
                                             force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                                             worker_id, enabled_worker_ids, dynamic_threshold)
        
        # Static mode - enhanced with health monitoring and retry logic
        return self._process_worker_static_sync(upscaled_image, model, positive, negative, vae,
                                               seed, steps, cfg, sampler_name, scheduler, denoise,
                                               tile_width, tile_height, padding, mask_blur,
                                               force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                                               worker_id, enabled_workers)
    
    def process_master(self, upscaled_image, model, positive, negative, vae,
                      seed, steps, cfg, sampler_name, scheduler, denoise,
                      tile_width, tile_height, padding, mask_blur,
                      force_uniform_tiles, tiled_decode, multi_job_id, enabled_worker_ids, 
                      dynamic_threshold):
        """Unified master processing with enhanced monitoring and failure handling."""
        # Round tile dimensions
        tile_width = self.round_to_multiple(tile_width)
        tile_height = self.round_to_multiple(tile_height)
        
        # Get image dimensions and batch size
        batch_size, height, width, _ = upscaled_image.shape
        
        # Calculate all tiles and grid
        all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)
        num_tiles_per_image = len(all_tiles)
        rows = math.ceil(height / tile_height)
        cols = math.ceil(width / tile_width)
        log(
            f"USDU Dist: Canvas {width}x{height} | Tile {tile_width}x{tile_height} | Grid {rows}x{cols} ({num_tiles_per_image} tiles/image) | Batch {batch_size}"
        )
        
        # Parse enabled workers
        enabled_workers = json.loads(enabled_worker_ids)
        num_workers = len(enabled_workers)
        
        # Determine processing mode
        mode = self._determine_processing_mode(batch_size, num_workers, dynamic_threshold)
        # Prefer tile-based static distribution when workers are available and there are multiple tiles,
        # even for batch == 1, to spread tiles across GPUs like the legacy dynamic tile queue.
        if num_workers > 0 and num_tiles_per_image > 1:
            mode = "static"
        
        log(f"USDU Dist: Workers {num_workers} | Mode {mode} | Threshold {dynamic_threshold}")

        if mode == "single_gpu":
            # No workers, process all tiles locally
            return self.process_single_gpu(upscaled_image, model, positive, negative, vae,
                                         seed, steps, cfg, sampler_name, scheduler, denoise,
                                         tile_width, tile_height, padding, mask_blur, force_uniform_tiles, tiled_decode)
        
        elif mode == "dynamic":
            # Dynamic mode for large batches
            return self.process_master_dynamic(upscaled_image, model, positive, negative, vae,
                                             seed, steps, cfg, sampler_name, scheduler, denoise,
                                             tile_width, tile_height, padding, mask_blur,
                                             force_uniform_tiles, tiled_decode, multi_job_id, enabled_workers)
        
        # Static mode - enhanced with unified job management
        return self._process_master_static_sync(upscaled_image, model, positive, negative, vae,
                                               seed, steps, cfg, sampler_name, scheduler, denoise,
                                               tile_width, tile_height, padding, mask_blur,
                                               force_uniform_tiles, tiled_decode, multi_job_id, enabled_workers,
                                               all_tiles, num_tiles_per_image)
    
    # Legacy static assignment helpers removed
    
    def _process_and_blend_tile(self, tile_idx, tile_pos, upscaled_image, result_image,
                               model, positive, negative, vae, seed, steps, cfg,
                               sampler_name, scheduler, denoise, tile_width, tile_height,
                               padding, mask_blur, image_width, image_height, force_uniform_tiles,
                               tiled_decode, batch_idx: int = 0):
        """Process a single tile and blend it into the result image."""
        x, y = tile_pos
        
        # Extract and process tile
        tile_tensor, x1, y1, ew, eh = self.extract_tile_with_padding(
            upscaled_image, x, y, tile_width, tile_height, padding, force_uniform_tiles
        )
        
        processed_tile = self.process_tile(tile_tensor, model, positive, negative, vae,
                                         seed, steps, cfg, sampler_name, 
                                         scheduler, denoise, tiled_decode, batch_idx=batch_idx,
                                         region=(x1, y1, x1 + ew, y1 + eh), image_size=(image_width, image_height))
        
        # Convert and blend
        processed_pil = tensor_to_pil(processed_tile, 0)
        # Create mask for this specific tile (no cache here; only used in single-tile path)
        tile_mask = self.create_tile_mask(image_width, image_height, x, y, tile_width, tile_height, mask_blur)
        # Use extraction position and size for blending
        result_image = self.blend_tile(result_image, processed_pil, 
                                     x1, y1, (ew, eh), tile_mask, padding)
        
        return result_image
    
    
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
    
    # Keep compatibility wrappers for existing code
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
    
    def round_to_multiple(self, value: int, multiple: int = 8) -> int:
        """Round value to nearest multiple."""
        return round(value / multiple) * multiple
    
    def calculate_tiles(self, image_width: int, image_height: int,
                       tile_width: int, tile_height: int, force_uniform_tiles: bool = True) -> List[Tuple[int, int]]:
        """Calculate tile positions to match Ultimate SD Upscale.

        Positions are a simple grid starting at (0,0) with steps of
        `tile_width` and `tile_height`, using ceil(rows/cols) to cover edges.
        Uniform vs non-uniform affects only crop/resize, not positions.
        """
        rows = math.ceil(image_height / tile_height)
        cols = math.ceil(image_width / tile_width)
        tiles: List[Tuple[int, int]] = []
        for yi in range(rows):
            for xi in range(cols):
                tiles.append((xi * tile_width, yi * tile_height))
        return tiles
    
    def extract_tile_with_padding(self, image: torch.Tensor, x: int, y: int,
                                 tile_width: int, tile_height: int, padding: int,
                                 force_uniform_tiles: bool) -> Tuple[torch.Tensor, int, int, int, int]:
        """Extract a tile region and resize to match USDU cropping logic.

        Mirrors ComfyUI_UltimateSDUpscale processing:
        - Build a mask with a white rectangle at the tile rect
        - Compute crop_region via get_crop_region(mask, padding)
        - If force_uniform_tiles: shrink/expand to target size of
          round_to_multiple(tile + padding) for each dimension
        - Else: target is ceil(crop_size/8)*8 per dimension
        - Extract the crop and resize to target tile_size
        Returns the resized tensor and crop origin/size for blending.
        """
        _, h, w, _ = image.shape

        # Create mask and compute initial padded crop region
        mask = Image.new('L', (w, h), 0)
        draw = ImageDraw.Draw(mask)
        draw.rectangle([x, y, x + tile_width, y + tile_height], fill=255)
        x1, y1, x2, y2 = get_crop_region(mask, padding)

        # Determine target tile size (processing size)
        if force_uniform_tiles:
            target_w = self.round_to_multiple(tile_width + padding, 8)
            target_h = self.round_to_multiple(tile_height + padding, 8)
            (x1, y1, x2, y2), (target_w, target_h) = expand_crop((x1, y1, x2, y2), w, h, target_w, target_h)
        else:
            crop_w = x2 - x1
            crop_h = y2 - y1
            target_w = max(8, math.ceil(crop_w / 8) * 8)
            target_h = max(8, math.ceil(crop_h / 8) * 8)
            (x1, y1, x2, y2), (target_w, target_h) = expand_crop((x1, y1, x2, y2), w, h, target_w, target_h)

        # Actual extracted size before resizing (for blending)
        extracted_width = x2 - x1
        extracted_height = y2 - y1

        # Extract tile and resize to processing size
        tile = image[:, y1:y2, x1:x2, :]
        tile_pil = tensor_to_pil(tile, 0)
        if tile_pil.size != (target_w, target_h):
            tile_pil = tile_pil.resize((target_w, target_h), Image.LANCZOS)

        tile_tensor = pil_to_tensor(tile_pil)
        if image.is_cuda:
            tile_tensor = tile_tensor.cuda()

        return tile_tensor, x1, y1, extracted_width, extracted_height

    def extract_batch_tile_with_padding(self, images: torch.Tensor, x: int, y: int,
                                        tile_width: int, tile_height: int, padding: int,
                                        force_uniform_tiles: bool) -> Tuple[torch.Tensor, int, int, int, int]:
        """Extract a tile region for the entire batch and resize to USDU logic.

        - Computes a single crop region from a mask at (x,y,w,h) with padding
        - force_uniform_tiles controls target processing size logic
        - Returns a batched tensor [B,H',W',C] and crop origin/size for blending
        """
        batch, h, w, _ = images.shape

        # Create mask and compute initial padded crop region (same for all images)
        mask = Image.new('L', (w, h), 0)
        draw = ImageDraw.Draw(mask)
        draw.rectangle([x, y, x + tile_width, y + tile_height], fill=255)
        x1, y1, x2, y2 = get_crop_region(mask, padding)

        # Determine target processing size
        if force_uniform_tiles:
            target_w = self.round_to_multiple(tile_width + padding, 8)
            target_h = self.round_to_multiple(tile_height + padding, 8)
            (x1, y1, x2, y2), (target_w, target_h) = expand_crop((x1, y1, x2, y2), w, h, target_w, target_h)
        else:
            crop_w = x2 - x1
            crop_h = y2 - y1
            target_w = max(8, math.ceil(crop_w / 8) * 8)
            target_h = max(8, math.ceil(crop_h / 8) * 8)
            (x1, y1, x2, y2), (target_w, target_h) = expand_crop((x1, y1, x2, y2), w, h, target_w, target_h)

        extracted_width = x2 - x1
        extracted_height = y2 - y1

        # Slice batch region
        tiles = images[:, y1:y2, x1:x2, :]

        # Resize each tile to target size
        resized_tiles = []
        for i in range(batch):
            tile_pil = tensor_to_pil(tiles, i)
            if tile_pil.size != (target_w, target_h):
                tile_pil = tile_pil.resize((target_w, target_h), Image.LANCZOS)
            resized_tiles.append(pil_to_tensor(tile_pil))
        tile_batch = torch.cat(resized_tiles, dim=0)

        if images.is_cuda:
            tile_batch = tile_batch.cuda()

        return tile_batch, x1, y1, extracted_width, extracted_height
    
    def process_tile(self, tile_tensor: torch.Tensor, model, positive, negative, vae,
                     seed: int, steps: int, cfg: float, sampler_name: str, 
                     scheduler: str, denoise: float, tiled_decode: bool = False,
                     batch_idx: int = 0, region: Tuple[int, int, int, int] = None,
                     image_size: Tuple[int, int] = None) -> torch.Tensor:
        """Process a single tile through SD sampling. 
        Note: positive and negative should already be pre-sliced for the current batch_idx."""
        debug_log(f"[process_tile] Processing tile for batch_idx={batch_idx}, seed={seed}, region={region}")
        
        
        # Import here to avoid circular dependencies
        from nodes import common_ksampler, VAEEncode, VAEDecode
        
        # Try to import tiled VAE nodes if available
        try:
            from nodes import VAEEncodeTiled, VAEDecodeTiled
            tiled_vae_available = True
        except ImportError:
            tiled_vae_available = False
            if tiled_decode:
                debug_log("Tiled VAE nodes not available, falling back to standard VAE")
        
        # Convert to PIL and back to ensure clean tensor without gradient tracking
        tile_pil = tensor_to_pil(tile_tensor, 0)
        clean_tensor = pil_to_tensor(tile_pil)
        
        # Ensure tensor is detached and doesn't require gradients
        clean_tensor = clean_tensor.detach()
        if hasattr(clean_tensor, 'requires_grad_'):
            clean_tensor.requires_grad_(False)
        
        # Move to correct device
        if tile_tensor.is_cuda:
            clean_tensor = clean_tensor.cuda()
            clean_tensor = clean_tensor.detach()  # Detach again after device transfer
        
        # Clone conditioning per tile (shares models, clones hints for cropping)
        positive_tile = clone_conditioning(positive, clone_hints=True)
        negative_tile = clone_conditioning(negative, clone_hints=True)
        
        # Crop conditioning to tile region if provided (assumes hints at image resolution)
        if region is not None and image_size is not None:
            init_size = image_size  # (width, height) of full image
            canvas_size = image_size
            tile_size = (tile_tensor.shape[2], tile_tensor.shape[1])  # (width, height)
            w_pad = 0  # No extra pad needed; region already includes padding
            h_pad = 0
            positive_cropped = crop_cond(positive_tile, region, init_size, canvas_size, tile_size, w_pad, h_pad)
            negative_cropped = crop_cond(negative_tile, region, init_size, canvas_size, tile_size, w_pad, h_pad)
        else:
            # No region cropping needed, use cloned conditioning as-is
            positive_cropped = positive_tile
            negative_cropped = negative_tile
        
        # Encode to latent (always non-tiled, matching original node)
        latent = VAEEncode().encode(vae, clean_tensor)[0]
        
        # Sample
        samples = common_ksampler(model, seed, steps, cfg, sampler_name, scheduler, 
                                positive_cropped, negative_cropped, latent, denoise=denoise)[0]
        
        # Decode back to image
        if tiled_decode and tiled_vae_available:
            image = VAEDecodeTiled().decode(vae, samples, tile_size=512)[0]
        else:
            image = VAEDecode().decode(vae, samples)[0]
        
        return image

    def process_tiles_batch(self, tile_batch: torch.Tensor, model, positive, negative, vae,
                            seed: int, steps: int, cfg: float, sampler_name: str,
                            scheduler: str, denoise: float, tiled_decode: bool,
                            region: Tuple[int, int, int, int], image_size: Tuple[int, int]) -> torch.Tensor:
        """Process a batch of tiles together (USDU behavior).

        tile_batch: [B, H, W, C]
        Returns image batch tensor [B, H, W, C]
        """
        # Import locally to avoid circular deps
        from nodes import common_ksampler, VAEEncode, VAEDecode
        try:
            from nodes import VAEEncodeTiled, VAEDecodeTiled
            tiled_vae_available = True
        except ImportError:
            tiled_vae_available = False

        # Detach and move device
        clean = tile_batch.detach()
        if hasattr(clean, 'requires_grad_'):
            clean.requires_grad_(False)
        if tile_batch.is_cuda:
            clean = clean.cuda().detach()

        # Clone/crop conditioning once for the region
        positive_tile = clone_conditioning(positive, clone_hints=True)
        negative_tile = clone_conditioning(negative, clone_hints=True)

        init_size = image_size
        canvas_size = image_size
        tile_size = (clean.shape[2], clean.shape[1])  # (W,H)
        w_pad = 0
        h_pad = 0
        positive_cropped = crop_cond(positive_tile, region, init_size, canvas_size, tile_size, w_pad, h_pad)
        negative_cropped = crop_cond(negative_tile, region, init_size, canvas_size, tile_size, w_pad, h_pad)

        # Encode -> Sample -> Decode
        latent = VAEEncode().encode(vae, clean)[0]
        samples = common_ksampler(model, seed, steps, cfg, sampler_name, scheduler,
                                  positive_cropped, negative_cropped, latent, denoise=denoise)[0]
        if tiled_decode and tiled_vae_available:
            image = VAEDecodeTiled().decode(vae, samples, tile_size=512)[0]
        else:
            image = VAEDecode().decode(vae, samples)[0]

        return image
    
    def create_tile_mask(self, image_width: int, image_height: int,
                        x: int, y: int, tile_width: int, tile_height: int, 
                        mask_blur: int) -> Image.Image:
        """Create a mask for blending tiles - matches Ultimate SD Upscale approach.
        
        Creates a black image with a white rectangle at the tile position,
        then applies blur to create soft edges.
        """
        # Create a full-size mask matching the image dimensions
        mask = Image.new('L', (image_width, image_height), 0)  # Black background
        
        # Draw white rectangle at tile position
        draw = ImageDraw.Draw(mask)
        draw.rectangle([x, y, x + tile_width, y + tile_height], fill=255)
        
        # Apply blur to soften edges
        if mask_blur > 0:
            mask = mask.filter(ImageFilter.GaussianBlur(mask_blur))
        
        return mask
    
    def blend_tile(self, base_image: Image.Image, tile_image: Image.Image,
                  x: int, y: int, extracted_size: Tuple[int, int], 
                  mask: Image.Image, padding: int) -> Image.Image:
        """Blend a processed tile back into the base image using Ultimate SD Upscale's exact approach.
        
        This follows the exact method from ComfyUI_UltimateSDUpscale/modules/processing.py
        """
        extracted_width, extracted_height = extracted_size
        
        # Debug logging (uncomment if needed)
        # debug_log(f"[Blend] Placing tile at ({x}, {y}), size: {extracted_width}x{extracted_height}")
        
        # Calculate the crop region that was used for extraction
        crop_region = (x, y, x + extracted_width, y + extracted_height)
        
        # The mask is already full-size, no need to crop
        
        # Resize the processed tile back to the extracted size
        if tile_image.size != (extracted_width, extracted_height):
            tile_resized = tile_image.resize((extracted_width, extracted_height), Image.LANCZOS)
        else:
            tile_resized = tile_image
        
        # Follow Ultimate SD Upscale blending approach:
        # Put the tile into position
        image_tile_only = Image.new('RGBA', base_image.size)
        image_tile_only.paste(tile_resized, crop_region[:2])
        
        # Add the mask as an alpha channel
        # Must make a copy due to the possibility of an edge becoming black
        temp = image_tile_only.copy()
        temp.putalpha(mask)  # Use the full image mask
        image_tile_only.paste(temp, image_tile_only)
        
        # Add back the tile to the initial image according to the mask in the alpha channel
        result = base_image.convert('RGBA')
        result.alpha_composite(image_tile_only)
        
        # Convert back to RGB
        return result.convert('RGB')
    
    def _slice_conditioning(self, positive, negative, batch_idx):
        """Helper to slice conditioning for a specific batch index."""
        # Clone and slice conditioning properly, including ControlNet hints
        positive_sliced = clone_conditioning(positive)
        negative_sliced = clone_conditioning(negative)
        
        for cond_list in [positive_sliced, negative_sliced]:
            for i in range(len(cond_list)):
                emb, cond_dict = cond_list[i]
                if emb.shape[0] > 1:
                    cond_list[i][0] = emb[batch_idx:batch_idx+1]
                if 'control' in cond_dict:
                    control = cond_dict['control']
                    while control is not None:
                        hint = control.cond_hint_original
                        if hint.shape[0] > 1:
                            control.cond_hint_original = hint[batch_idx:batch_idx+1]
                        control = control.previous_controlnet
                if 'mask' in cond_dict and cond_dict['mask'].shape[0] > 1:
                    cond_dict['mask'] = cond_dict['mask'][batch_idx:batch_idx+1]
        
        return positive_sliced, negative_sliced
    
    async def _get_all_completed_tasks(self, multi_job_id):
        """Helper to retrieve all completed tasks from the job data."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if job_data and JOB_COMPLETED_TASKS in job_data:
                return dict(job_data[JOB_COMPLETED_TASKS])  # Return a copy
            return {}
    
    # Note: Removed unused _process_worker_static_async to reduce redundancy
    
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
    
    def _process_single_tile(self, global_idx, num_tiles_per_image, upscaled_image, all_tiles,
                                  model, positive, negative, vae, seed, steps, cfg, sampler_name,
                                  scheduler, denoise, tiled_decode, tile_width, tile_height, padding,
                                  width, height, force_uniform_tiles, sliced_conditioning_cache):
        """Process a single tile."""
        # Calculate which image and tile this corresponds to
        batch_idx = global_idx // num_tiles_per_image
        tile_idx = global_idx % num_tiles_per_image
        
        # Skip if batch_idx is out of range
        if batch_idx >= upscaled_image.shape[0]:
            debug_log(f"Warning: Calculated batch_idx {batch_idx} exceeds batch size {upscaled_image.shape[0]}")
            return None
        
        # Get or create sliced conditioning for this batch index
        if batch_idx not in sliced_conditioning_cache:
            positive_sliced, negative_sliced = self._slice_conditioning(positive, negative, batch_idx)
            sliced_conditioning_cache[batch_idx] = (positive_sliced, negative_sliced)
        else:
            positive_sliced, negative_sliced = sliced_conditioning_cache[batch_idx]
        
        x, y = all_tiles[tile_idx]
        
        # Extract tile from the specific image in the batch
        tile_tensor, x1, y1, ew, eh = self.extract_tile_with_padding(
            upscaled_image[batch_idx:batch_idx+1], x, y, tile_width, tile_height, padding, force_uniform_tiles
        )
        
        # Process tile through SD with unique seed
        image_seed = seed + batch_idx * 1000
        processed_tile = self.process_tile(tile_tensor, model, positive_sliced, negative_sliced, vae,
                                         image_seed, steps, cfg, sampler_name,
                                         scheduler, denoise, tiled_decode, batch_idx=batch_idx,
                                         region=(x1, y1, x1 + ew, y1 + eh), image_size=(width, height))
        
        return {
            'tile': processed_tile,
            'global_idx': global_idx,
            'batch_idx': batch_idx,
            'tile_idx': tile_idx,
            'x': x1,
            'y': y1,
            'extracted_width': ew,
            'extracted_height': eh
        }
    
    async def send_tiles_batch_to_master(self, processed_tiles, multi_job_id, master_url, 
                                       padding, worker_id):
        """Send all processed tiles to master, chunked if large."""
        if not processed_tiles:
            return  # Early exit if empty

        total_tiles = len(processed_tiles)
        debug_log(f"Worker[{worker_id[:8]}] - Preparing to send {total_tiles} tiles (size-aware chunks)")

        # Prepare encoded images and sizes to enable size-aware chunking
        encoded = []
        for idx, tile_data in enumerate(processed_tiles):
            img = tensor_to_pil(tile_data['tile'], 0)
            bio = io.BytesIO()
            # Keep compression low to balance speed and size; adjust if needed
            img.save(bio, format='PNG', compress_level=0)
            raw = bio.getvalue()
            encoded.append({
                'bytes': raw,
                'meta': {
                    'tile_idx': tile_data['tile_idx'],
                    'x': tile_data['x'],
                    'y': tile_data['y'],
                    'extracted_width': tile_data['extracted_width'],
                    'extracted_height': tile_data['extracted_height'],
                    **({'batch_idx': tile_data['batch_idx']} if 'batch_idx' in tile_data else {}),
                    **({'global_idx': tile_data['global_idx']} if 'global_idx' in tile_data else {}),
                }
            })

        # Size-aware chunking
        max_bytes = int(MAX_PAYLOAD_SIZE) - (1024 * 1024)  # 1MB headroom
        i = 0
        chunk_index = 0
        while i < total_tiles:
            data = aiohttp.FormData()
            data.add_field('multi_job_id', multi_job_id)
            data.add_field('worker_id', str(worker_id))
            data.add_field('padding', str(padding))

            metadata = []
            used = 0
            j = i
            while j < total_tiles:
                img_bytes = encoded[j]['bytes']
                meta = encoded[j]['meta']
                # Rough overhead for fields + JSON
                overhead = 1024
                if used + len(img_bytes) + overhead > max_bytes and j > i:
                    break
                # Accept this tile in this chunk
                metadata.append(meta)
                data.add_field(f'tile_{j - i}', io.BytesIO(img_bytes), filename=f'tile_{j}.png', content_type='image/png')
                used += len(img_bytes) + overhead
                j += 1

            # Ensure at least one tile per chunk
            if j == i:
                # Single oversized tile, send anyway
                meta = encoded[j]['meta']
                metadata.append(meta)
                data.add_field('tile_0', io.BytesIO(encoded[j]['bytes']), filename=f'tile_{j}.png', content_type='image/png')
                j += 1

            chunk_size = j - i
            is_chunk_last = (j >= total_tiles)
            data.add_field('is_last', str(is_chunk_last))
            data.add_field('batch_size', str(chunk_size))
            data.add_field('tiles_metadata', json.dumps(metadata), content_type='application/json')

            # Retry logic with exponential backoff
            max_retries = 5
            retry_delay = 0.5
            for attempt in range(max_retries):
                try:
                    session = await get_client_session()
                    url = f"{master_url}/distributed/submit_tiles"
                    async with session.post(url, data=data) as response:
                        response.raise_for_status()
                        break
                except Exception as e:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 5.0)
                    else:
                        log(f"UltimateSDUpscale Worker - Failed to send chunk {chunk_index} after {max_retries} attempts: {e}")
                        raise

            debug_log(f"Worker[{worker_id[:8]}] - Sent chunk {chunk_index} ({chunk_size} tiles, ~{used/1e6:.2f} MB)")
            chunk_index += 1
            i = j

    def process_single_gpu(self, upscaled_image, model, positive, negative, vae,
                          seed, steps, cfg, sampler_name, scheduler, denoise,
                          tile_width, tile_height, padding, mask_blur, force_uniform_tiles, tiled_decode):
        """Process all tiles on a single GPU (no distribution), batching per tile like USDU."""
        # Round tile dimensions
        tile_width = self.round_to_multiple(tile_width)
        tile_height = self.round_to_multiple(tile_height)

        # Get image dimensions and batch size
        batch_size, height, width, _ = upscaled_image.shape

        # Calculate all tiles
        all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)

        rows = math.ceil(height / tile_height)
        cols = math.ceil(width / tile_width)
        log(
            f"USDU Dist: Single GPU | Canvas {width}x{height} | Tile {tile_width}x{tile_height} | Grid {rows}x{cols} ({len(all_tiles)} tiles/image) | Batch {batch_size}"
        )

        # Prepare result images list
        result_images = []
        for b in range(batch_size):
            image_pil = tensor_to_pil(upscaled_image[b:b+1], 0).convert('RGB')
            result_images.append(image_pil.copy())

        # Precompute tile masks once
        tile_masks = []
        for tx, ty in all_tiles:
            tile_masks.append(self.create_tile_mask(width, height, tx, ty, tile_width, tile_height, mask_blur))

        # Process tiles batched across images
        for tile_idx, (tx, ty) in enumerate(all_tiles):
            # Extract batched tile
            tile_batch, x1, y1, ew, eh = self.extract_batch_tile_with_padding(
                upscaled_image, tx, ty, tile_width, tile_height, padding, force_uniform_tiles
            )

            # Process batch
            region = (x1, y1, x1 + ew, y1 + eh)
            processed_batch = self.process_tiles_batch(tile_batch, model, positive, negative, vae,
                                                       seed, steps, cfg, sampler_name, scheduler, denoise,
                                                       tiled_decode, region, (width, height))

            # Blend results back into each image using cached mask
            tile_mask = tile_masks[tile_idx]
            for b in range(batch_size):
                tile_pil = tensor_to_pil(processed_batch, b)
                # Resize back to extracted size
                if tile_pil.size != (ew, eh):
                    tile_pil = tile_pil.resize((ew, eh), Image.LANCZOS)
                result_images[b] = self.blend_tile(result_images[b], tile_pil, x1, y1, (ew, eh), tile_mask, padding)

        # Convert back to tensor
        result_tensors = [pil_to_tensor(img) for img in result_images]
        result_tensor = torch.cat(result_tensors, dim=0)
        if upscaled_image.is_cuda:
            result_tensor = result_tensor.cuda()

        return (result_tensor,)
    
    def process_master_dynamic(self, upscaled_image, model, positive, negative, vae,
                              seed, steps, cfg, sampler_name, scheduler, denoise,
                              tile_width, tile_height, padding, mask_blur,
                              force_uniform_tiles, tiled_decode, multi_job_id, enabled_workers):
        """Dynamic mode for large batches - assigns whole images to workers dynamically, including master."""
        # Get batch size and dimensions
        batch_size, height, width, _ = upscaled_image.shape
        num_workers = len(enabled_workers)
        
        log(f"USDU Dist: Image queue distribution | Batch {batch_size} | Workers {num_workers} | Canvas {width}x{height} | Tile {tile_width}x{tile_height}")

        # No fixed share - all images are dynamic
        all_indices = list(range(batch_size))
        
        debug_log(f"Processing {batch_size} images dynamically across master + {num_workers} workers.")
        
        # Calculate tiles for processing
        all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)
        
        # Initialize job queue for communication
        try:
            run_async_in_server_loop(
                init_dynamic_job(multi_job_id, batch_size, enabled_workers, all_indices),
                timeout=2.0
            )
        except Exception as e:
            debug_log(f"UltimateSDUpscale Master - Queue initialization error: {e}")
            raise RuntimeError(f"Failed to initialize dynamic mode queue: {e}")
        
        # Convert batch to PIL list
        result_images = [tensor_to_pil(upscaled_image[b:b+1], 0).convert('RGB').copy() for b in range(batch_size)]
        
        # Process images dynamically with master participating
        prompt_server = ensure_tile_jobs_initialized()
        processed_count = 0
        consecutive_retries = 0
        max_consecutive_retries = 10
        
        # Process loop - master pulls from queue and processes synchronously
        while processed_count < batch_size:
            # Try to get an image to process
            image_idx = run_async_in_server_loop(
                self._get_next_image_index(multi_job_id),
                timeout=5.0  # Short timeout to allow frequent checks
            )

            if image_idx is not None:
                # Reset retry counter and process locally
                consecutive_retries = 0
                debug_log(f"Master processing image {image_idx} dynamically")
                processed_count += 1

                # Process locally
                single_tensor = upscaled_image[image_idx:image_idx+1]
                local_image = result_images[image_idx]
                image_seed = seed + image_idx * 1000
                
                # Pre-slice conditioning once per image (not per tile)
                positive_sliced = clone_conditioning(positive)
                negative_sliced = clone_conditioning(negative)
                for cond_list in [positive_sliced, negative_sliced]:
                    for i in range(len(cond_list)):
                        emb, cond_dict = cond_list[i]
                        if emb.shape[0] > 1:
                            cond_list[i][0] = emb[image_idx:image_idx+1]
                        if 'control' in cond_dict:
                            control = cond_dict['control']
                            while control is not None:
                                hint = control.cond_hint_original
                                if hint.shape[0] > 1:
                                    control.cond_hint_original = hint[image_idx:image_idx+1]
                                control = control.previous_controlnet
                        if 'mask' in cond_dict and cond_dict['mask'].shape[0] > 1:
                            cond_dict['mask'] = cond_dict['mask'][image_idx:image_idx+1]
                
                for tile_idx, pos in enumerate(all_tiles):
                    local_image = self._process_and_blend_tile(
                        tile_idx, pos, single_tensor, local_image,
                        model, positive_sliced, negative_sliced, vae, image_seed, steps, cfg,
                        sampler_name, scheduler, denoise, tile_width, tile_height,
                        padding, mask_blur, width, height, force_uniform_tiles,
                        tiled_decode, batch_idx=image_idx
                    )
                    
                    # Yield after each tile to minimize worker downtime
                    run_async_in_server_loop(self._async_yield(), timeout=0.1)
                    # Note: No per-tile drain here – that's what makes this "per-image"
                
                result_images[image_idx] = local_image
                
                # Mark as completed
                run_async_in_server_loop(
                    self._mark_image_completed(multi_job_id, image_idx, local_image),
                    timeout=5.0
                )
                
                # NEW: Drain after the full image is marked complete (catches workers who finished during master's processing)
                drained_count = run_async_in_server_loop(
                    self._drain_worker_results_queue(multi_job_id),
                    timeout=5.0
                )
                
                if drained_count > 0:
                    debug_log(f"Drained {drained_count} worker images after master's image {image_idx}")
                
                # NEW: Log overall progress (includes master's image + any drained workers)
                completed_now = run_async_in_server_loop(
                    self._get_total_completed_count(multi_job_id),
                    timeout=1.0
                )
                log(f"USDU Dist: Images progress {completed_now}/{batch_size}")
                
                # Yield to allow workers to get new images after completing one
                run_async_in_server_loop(self._async_yield(), timeout=0.1)
            else:
                # Queue empty: collect any queued worker results to update progress
                drained_count = run_async_in_server_loop(
                    self._drain_worker_results_queue(multi_job_id),
                    timeout=5.0
                )
                run_async_in_server_loop(self._async_yield(), timeout=0.1)  # Yield after drain
                
                # Check for timed out workers and requeue their images
                requeued_count = run_async_in_server_loop(
                    self._check_and_requeue_timed_out_workers(multi_job_id, batch_size),
                    timeout=5.0
                )
                run_async_in_server_loop(self._async_yield(), timeout=0.1)  # Yield after requeue
                
                if requeued_count > 0:
                    log(f"Requeued {requeued_count} images from timed out workers")
                    consecutive_retries = 0  # Reset since we have work to do
                    continue

                # Now check total completed (includes newly collected)
                completed_now = run_async_in_server_loop(
                    self._get_total_completed_count(multi_job_id),
                    timeout=1.0
                )
                
                log(f"USDU Dist: Images progress {completed_now}/{batch_size}")
                
                if completed_now >= batch_size:
                    break

                run_async_in_server_loop(self._async_yield(), timeout=0.1)  # Yield before pending check
                
                # Check if there are pending images in the queue (could be requeued)
                pending_count = run_async_in_server_loop(
                    self._get_pending_count(multi_job_id),
                    timeout=1.0
                )
                
                if pending_count > 0:
                    consecutive_retries = 0  # Reset retries since there's work to do
                    continue

                consecutive_retries += 1
                if consecutive_retries >= max_consecutive_retries:
                    log(f"Max retries ({max_consecutive_retries}) reached. Forcing collection of remaining results.")
                    break  # Force exit to collection phase

                debug_log("Waiting for workers")
                # Use async sleep to allow event loop to process worker requests
                run_async_in_server_loop(asyncio.sleep(2), timeout=3.0)
        
        debug_log(f"Master processed {processed_count} images locally")
        
        # Get all completed images to check what needs to be collected
        all_completed = run_async_in_server_loop(
            self._get_all_completed_images(multi_job_id),
            timeout=5.0
        )
        
        # Calculate how many we still need to collect
        remaining_to_collect = batch_size - len(all_completed)
        
        if remaining_to_collect > 0:
            debug_log(f"Waiting for {remaining_to_collect} more images from workers")
            # Use the unified worker timeout for the collection phase
            collection_timeout = float(get_worker_timeout_seconds())
            collected_images = run_async_in_server_loop(
                self._async_collect_dynamic_images(multi_job_id, remaining_to_collect, num_workers, batch_size, processed_count),
                timeout=collection_timeout
            )
            
            # Merge collected with already completed
            all_completed.update(collected_images)
        
        # Update result images with all completed images
        for idx, processed_img in all_completed.items():
            if idx < batch_size:
                result_images[idx] = processed_img
        
        # Convert back to tensor
        result_tensor = torch.cat([pil_to_tensor(img) for img in result_images], dim=0) if batch_size > 1 else pil_to_tensor(result_images[0])
        if upscaled_image.is_cuda:
            result_tensor = result_tensor.cuda()
        
        debug_log(f"UltimateSDUpscale Master - Job {multi_job_id} complete")
        log(f"Completed processing all {batch_size} images")
        return (result_tensor,)
    
    
    async def _async_yield(self):
        """Simple async yield to allow event loop processing."""
        await asyncio.sleep(0)
    
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
        return await _check_and_requeue_timed_out_workers(multi_job_id, batch_size)
    
    
    def process_worker_dynamic(self, upscaled_image, model, positive, negative, vae,
                               seed, steps, cfg, sampler_name, scheduler, denoise,
                               tile_width, tile_height, padding, mask_blur,
                               force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                               worker_id, enabled_worker_ids, dynamic_threshold):
        """Worker processing in dynamic mode - processes whole images."""
        # Round tile dimensions
        tile_width = self.round_to_multiple(tile_width)
        tile_height = self.round_to_multiple(tile_height)

        # Get dimensions and tile grid
        batch_size, height, width, _ = upscaled_image.shape
        all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)
        log(f"USDU Dist Worker[{worker_id[:8]}]: Processing image queue | Batch {batch_size}")

        # Keep track of processed images for is_last detection
        processed_count = 0

        # Poll for job readiness to avoid races during master init
        max_poll_attempts = 20  # ~20s at 1s sleep
        for attempt in range(max_poll_attempts):
            ready = run_async_in_server_loop(
                self._check_job_status(multi_job_id, master_url),
                timeout=5.0
            )
            if ready:
                debug_log(f"Worker[{worker_id[:8]}] job {multi_job_id} ready after {attempt} attempts")
                break
            time.sleep(1.0)  # Poll every 1s
        else:
            log(f"Job {multi_job_id} not ready after {max_poll_attempts} attempts, aborting")
            return (upscaled_image,)

        # Loop to request and process images
        while True:
            # Request an image to process
            image_idx, estimated_remaining = run_async_in_server_loop(
                self._request_image_from_master(multi_job_id, master_url, worker_id),
                timeout=TILE_WAIT_TIMEOUT
            )

            if image_idx is None:
                debug_log(f"USDU Dist Worker - No more images to process")
                break

            debug_log(f"Worker[{worker_id[:8]}] - Assigned image {image_idx}")
            processed_count += 1

            # Determine if this should be marked as last for this worker
            is_last_for_worker = (estimated_remaining == 0)

            # Extract single image tensor
            single_tensor = upscaled_image[image_idx:image_idx+1]

            # Convert to PIL for processing
            local_image = tensor_to_pil(single_tensor, 0).copy()

            # Process all tiles for this image
            image_seed = seed + image_idx * 1000

            # Pre-slice conditioning once per image (not per tile)
            positive_sliced = clone_conditioning(positive)
            negative_sliced = clone_conditioning(negative)
            for cond_list in [positive_sliced, negative_sliced]:
                for i in range(len(cond_list)):
                        emb, cond_dict = cond_list[i]
                        if emb.shape[0] > 1:
                            cond_list[i][0] = emb[image_idx:image_idx+1]
                        if 'control' in cond_dict:
                            control = cond_dict['control']
                            while control is not None:
                                hint = control.cond_hint_original
                                if hint.shape[0] > 1:
                                    control.cond_hint_original = hint[image_idx:image_idx+1]
                                control = control.previous_controlnet
                        if 'mask' in cond_dict and cond_dict['mask'].shape[0] > 1:
                            cond_dict['mask'] = cond_dict['mask'][image_idx:image_idx+1]

                for tile_idx, pos in enumerate(all_tiles):
                    local_image = self._process_and_blend_tile(
                        tile_idx, pos, single_tensor, local_image,
                        model, positive_sliced, negative_sliced, vae, image_seed, steps, cfg,
                        sampler_name, scheduler, denoise, tile_width, tile_height,
                        padding, mask_blur, width, height, force_uniform_tiles,
                        tiled_decode, batch_idx=image_idx
                    )
                    run_async_in_server_loop(
                        _send_heartbeat_to_master(multi_job_id, master_url, worker_id),
                        timeout=5.0
                    )

                # Send processed image back to master
                try:
                    # Use the estimated remaining to determine if this is the last image
                    is_last = is_last_for_worker
                    run_async_in_server_loop(
                        self._send_full_image_to_master(local_image, image_idx, multi_job_id,
                                                        master_url, worker_id, is_last),
                        timeout=TILE_SEND_TIMEOUT
                    )
                    # Send heartbeat after processing
                    run_async_in_server_loop(
                        _send_heartbeat_to_master(multi_job_id, master_url, worker_id),
                        timeout=5.0
                    )
                    if is_last:
                        break
                except Exception as e:
                    log(f"USDU Dist Worker[{worker_id[:8]}] - Error sending image {image_idx}: {e}")
                    # Continue processing other images

        # Send final is_last signal
        debug_log(f"Worker[{worker_id[:8]}] processed {processed_count} images, sending completion signal")
        try:
            run_async_in_server_loop(
                self._send_worker_complete_signal(multi_job_id, master_url, worker_id),
                timeout=TILE_SEND_TIMEOUT
            )
        except Exception as e:
            log(f"USDU Dist Worker[{worker_id[:8]}] - Error sending completion signal: {e}")

        return (upscaled_image,)
    
    async def _request_image_from_master(self, multi_job_id, master_url, worker_id):
        """Request an image index to process from master in dynamic mode."""
        # Enhanced retries with 404-specific delay, backoff cap, and total timeout to handle init races
        max_retries = 10
        retry_delay = 0.5
        start_time = time.time()
        
        for attempt in range(max_retries):
            # Check total timeout
            if time.time() - start_time > 30:
                log(f"Total request timeout after 30s for worker {worker_id}")
                return None, 0
                
            try:
                session = await get_client_session()
                url = f"{master_url}/distributed/request_image"
                
                async with session.post(url, json={
                    'worker_id': str(worker_id),
                    'multi_job_id': multi_job_id
                }) as response:
                    if response.status == 200:
                        data = await response.json()
                        image_idx = data.get('image_idx')
                        estimated_remaining = data.get('estimated_remaining', 0)
                        return image_idx, estimated_remaining
                    elif response.status == 404:
                        # Special handling for 404 - job not found yet
                        text = await response.text()
                        debug_log(f"Job not found (404), will retry: {text}")
                        await asyncio.sleep(1.0)
                    else:
                        text = await response.text()
                        debug_log(f"Request image failed: {response.status} - {text}")
                        
            except Exception as e:
                if attempt < max_retries - 1:
                    debug_log(f"Retry {attempt + 1}/{max_retries} after error: {e}")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    retry_delay = min(retry_delay, 5.0)  # Cap backoff at 5s
                else:
                    log(f"Failed to request image after {max_retries} attempts: {e}")
                    raise
        
        return None, 0
    
    async def _request_tile_from_master(self, multi_job_id, master_url, worker_id):
        """Request a tile index to process from master in static mode (reusing dynamic infrastructure)."""
        # Reuse the same retry logic as dynamic mode
        max_retries = 10
        retry_delay = 0.5
        start_time = time.time()
        
        for attempt in range(max_retries):
            # Check total timeout
            if time.time() - start_time > 30:
                log(f"Total request timeout after 30s for worker {worker_id}")
                return None, 0
                
            try:
                session = await get_client_session()
                url = f"{master_url}/distributed/request_image"  # Same endpoint
                
                async with session.post(url, json={
                    'worker_id': str(worker_id),
                    'multi_job_id': multi_job_id
                }) as response:
                    if response.status == 200:
                        data = await response.json()
                        tile_idx = data.get('tile_idx')
                        estimated_remaining = data.get('estimated_remaining', 0)
                        batched_static = data.get('batched_static', False)
                        return tile_idx, estimated_remaining, batched_static
                    elif response.status == 404:
                        # Special handling for 404 - job not found yet
                        text = await response.text()
                        debug_log(f"Job not found (404), will retry: {text}")
                        await asyncio.sleep(1.0)
                    else:
                        text = await response.text()
                        debug_log(f"Request tile failed: {response.status} - {text}")
                        
            except Exception as e:
                if attempt < max_retries - 1:
                    debug_log(f"Retry {attempt + 1}/{max_retries} after error: {e}")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    retry_delay = min(retry_delay, 5.0)  # Cap backoff at 5s
                else:
                    log(f"Failed to request tile after {max_retries} attempts: {e}")
                    raise
        
        return None, 0, False
    
    async def _check_job_status(self, multi_job_id, master_url):
        """Check if job is ready on the master."""
        try:
            session = await get_client_session()
            url = f"{master_url}/distributed/job_status?multi_job_id={multi_job_id}"
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('ready', False)
                return False
        except Exception as e:
            debug_log(f"Job status check failed: {e}")
            return False
    
    async def _send_full_image_to_master(self, image_pil, image_idx, multi_job_id, 
                                        master_url, worker_id, is_last):
        """Send a processed full image back to master in dynamic mode."""
        # Serialize image to PNG
        byte_io = io.BytesIO()
        image_pil.save(byte_io, format='PNG', compress_level=0)
        byte_io.seek(0)
        
        # Prepare form data
        data = aiohttp.FormData()
        data.add_field('multi_job_id', multi_job_id)
        data.add_field('worker_id', str(worker_id))
        data.add_field('image_idx', str(image_idx))
        data.add_field('is_last', str(is_last))
        data.add_field('full_image', byte_io, filename=f'image_{image_idx}.png', 
                      content_type='image/png')
        
        # Retry logic
        max_retries = 5
        retry_delay = 0.5
        
        for attempt in range(max_retries):
            try:
                session = await get_client_session()
                url = f"{master_url}/distributed/submit_image"
                
                async with session.post(url, data=data) as response:
                    response.raise_for_status()
                    debug_log(f"Successfully sent image {image_idx} to master")
                    return
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    debug_log(f"Retry {attempt + 1}/{max_retries} after error: {e}")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    log(f"Failed to send image {image_idx} after {max_retries} attempts: {e}")
                    raise
    
    # Note: Using _send_heartbeat_to_master from usdu_managment.py instead of duplicate
    
    async def _send_worker_complete_signal(self, multi_job_id, master_url, worker_id):
        """Send completion signal to master in dynamic mode."""
        # Send a dummy request with is_last=True
        data = aiohttp.FormData()
        data.add_field('multi_job_id', multi_job_id)
        data.add_field('worker_id', str(worker_id))
        data.add_field('is_last', 'true')
        # No image data - just completion signal
        
        session = await get_client_session()
        url = f"{master_url}/distributed/submit_image"
        
        async with session.post(url, data=data) as response:
            response.raise_for_status()
            debug_log(f"Worker {worker_id} sent completion signal")

    def _determine_processing_mode(self, batch_size: int, num_workers: int, dynamic_threshold: int) -> str:
        """Determines processing mode per requested policy:
        - any workers     => prefer static (tile-based) for USDU
        - no workers      => single_gpu
        """
        if num_workers == 0:
            return "single_gpu"
        # Default to static when distributed; master/worker may still override if special cases arise
        return "static"

# Ensure initialization before registering routes
ensure_tile_jobs_initialized()

# Node registration
NODE_CLASS_MAPPINGS = {
    "UltimateSDUpscaleDistributed": UltimateSDUpscaleDistributed,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "UltimateSDUpscaleDistributed": "Ultimate SD Upscale Distributed (No Upscale)",
}
