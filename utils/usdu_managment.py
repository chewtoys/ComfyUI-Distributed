import asyncio
import time
import json
import copy
import os
import io
from aiohttp import web
import server
from PIL import Image

# Import from other utilities
from .logging import debug_log, log
from .network import build_worker_url, handle_api_error, get_client_session, probe_worker
from ..upscale.job_models import BaseJobState, ImageJobState, TileJobState
# We avoid converting to tensors on the master for tiles; blending uses PIL
from typing import List, Optional

# Configure maximum payload size (50MB default, configurable via environment variable)
MAX_PAYLOAD_SIZE = int(os.environ.get('COMFYUI_MAX_PAYLOAD_SIZE', str(50 * 1024 * 1024)))

# Import HEARTBEAT_TIMEOUT from constants
from .constants import HEARTBEAT_TIMEOUT
from .config import load_config


def _parse_tiles_from_form(data):
    """Parse tiles submitted via multipart/form-data into a list of tile dicts.

    Expects the following fields in the aiohttp form data:
    - 'tiles_metadata': JSON list with per-tile metadata items containing at least
      'tile_idx', 'x', 'y', 'extracted_width', 'extracted_height'. Optional
      'batch_idx' and 'global_idx' are included when available.
    - 'tile_{i}': image bytes for each tile described in tiles_metadata (PNG).
    - 'padding': integer padding used during extraction (optional; defaults 0).

    Returns: list of dicts with keys: 'image', 'tile_idx', 'x', 'y',
    'extracted_width', 'extracted_height', and optional 'batch_idx', 'global_idx',
    plus 'padding'.
    """
    try:
        # Parse padding if present
        padding = int(data.get('padding', 0)) if data.get('padding') is not None else 0
    except Exception:
        padding = 0

    # Parse tiles metadata (JSON list)
    meta_raw = data.get('tiles_metadata')
    if meta_raw is None:
        raise ValueError("Missing tiles_metadata")

    try:
        metadata = json.loads(meta_raw)
    except Exception as e:
        raise ValueError(f"Invalid tiles_metadata JSON: {e}")

    if not isinstance(metadata, list):
        raise ValueError("tiles_metadata must be a list")

    tiles = []
    # Iterate over metadata items and corresponding uploaded files tile_0, tile_1, ...
    for i, meta in enumerate(metadata):
        file_field = data.get(f'tile_{i}')
        if file_field is None or not hasattr(file_field, 'file'):
            raise ValueError(f"Missing tile data for index {i}")

        # Read image bytes and decode to PIL
        raw = file_field.file.read()
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception as e:
            raise ValueError(f"Invalid image data for tile {i}: {e}")

        # Build tile dictionary (store PIL only; master blends via PIL)
        try:
            tile_info = {
                'image': img,
                'tile_idx': int(meta.get('tile_idx', i)),
                'x': int(meta.get('x', 0)),
                'y': int(meta.get('y', 0)),
                'extracted_width': int(meta.get('extracted_width', img.width)),
                'extracted_height': int(meta.get('extracted_height', img.height)),
                'padding': int(padding),
            }
        except Exception as e:
            raise ValueError(f"Invalid metadata values for tile {i}: {e}")

        # Optional fields
        if 'batch_idx' in meta:
            try:
                tile_info['batch_idx'] = int(meta['batch_idx'])
            except Exception:
                pass
        if 'global_idx' in meta:
            try:
                tile_info['global_idx'] = int(meta['global_idx'])
            except Exception:
                pass

        tiles.append(tile_info)

    return tiles

async def init_dynamic_job(multi_job_id: str, batch_size: int, enabled_workers: List[str], all_indices: Optional[List[int]] = None):
    """Initialize queue for dynamic mode (per-image), with collector fields.

    - Creates pending image queue entries
    - Initializes dynamic completion/status tracking
    """
    await _init_job_queue(
        multi_job_id,
        'dynamic',
        batch_size=batch_size,
        all_indices=all_indices or list(range(batch_size)),
        enabled_workers=enabled_workers,
    )
    debug_log(f"Job {multi_job_id} initialized with {batch_size} images")


async def init_static_job_batched(multi_job_id: str, batch_size: int, num_tiles_per_image: int, enabled_workers: List[str]):
    """Initialize queue for static mode (batched-per-tile).

    - Populates pending tile queue with tile ids [0..num_tiles_per_image-1]
    """
    await _init_job_queue(
        multi_job_id,
        'static',
        batch_size=batch_size,
        num_tiles_per_image=num_tiles_per_image,
        enabled_workers=enabled_workers,
        batched_static=True,
    )
    # Initialization handled by master; avoid duplicate init logs here

async def _init_job_queue(
    multi_job_id,
    mode,
    batch_size=None,
    num_tiles_per_image=None,
    all_indices=None,
    enabled_workers=None,
    batched_static: bool = False,
):
    """Unified initialization for job queues in static and dynamic modes."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        if multi_job_id in prompt_server.distributed_pending_tile_jobs:
            debug_log(f"Queue already exists for {multi_job_id}")
            return

        if mode == 'dynamic':
            job_data = ImageJobState(multi_job_id=multi_job_id)
        elif mode == 'static':
            job_data = TileJobState(multi_job_id=multi_job_id)
        else:
            raise ValueError(f"Unknown mode: {mode}")

        job_data.worker_status = {w: time.time() for w in enabled_workers or []}
        job_data.assigned_to_workers = {w: [] for w in enabled_workers or []}

        if mode == 'dynamic':
            job_data.batch_size = int(batch_size or 0)
            pending_queue = job_data.pending_images
            for i in (all_indices or range(int(batch_size or 0))):
                await pending_queue.put(i)
            debug_log(f"Initialized image queue with {batch_size} pending items")
        elif mode == 'static':
            job_data.num_tiles_per_image = int(num_tiles_per_image or 0)
            job_data.batch_size = int(batch_size or 0)
            job_data.batched_static = bool(batched_static)
            # For batched static distribution, populate only tile ids [0..num_tiles_per_image-1]
            pending_queue = job_data.pending_tasks
            if batched_static and num_tiles_per_image is not None:
                for i in range(num_tiles_per_image):
                    await pending_queue.put(i)
            else:
                total_tiles = int(batch_size or 0) * int(num_tiles_per_image or 0)
                for i in range(total_tiles):
                    await pending_queue.put(i)
            
        prompt_server.distributed_pending_tile_jobs[multi_job_id] = job_data

async def _drain_results_queue(multi_job_id):
    """Drain pending results from queue and update completed_tasks. Returns count drained.

    Uses non-blocking get_nowait to avoid await timeouts and reduce latency.
    """
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
        if not isinstance(job_data, BaseJobState):
            return 0
        q = job_data.queue
        completed_tasks = job_data.completed_tasks

        collected = 0
        while True:
            try:
                result = q.get_nowait()
            except asyncio.QueueEmpty:
                break

            worker_id = result['worker_id']
            is_last = result.get('is_last', False)

            if 'image_idx' in result and 'image' in result:
                task_id = result['image_idx']
                if task_id not in completed_tasks:
                    completed_tasks[task_id] = result['image']
                    collected += 1
            elif 'tiles' in result:
                for tile_data in result['tiles']:
                    task_id = tile_data.get('global_idx', tile_data['tile_idx'])
                    if task_id not in completed_tasks:
                        completed_tasks[task_id] = tile_data
                        collected += 1
            if is_last:
                # Track worker completion
                if worker_id in job_data.worker_status:
                    del job_data.worker_status[worker_id]

        return collected

async def _check_and_requeue_timed_out_workers(multi_job_id, total_tasks):
    """Check timed out workers and requeue their tasks. Returns requeued count."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
        if not job_data:
            return 0

        current_time = time.time()
        if not isinstance(job_data, BaseJobState):
            return 0

        requeued_count = 0
        completed_tasks = job_data.completed_tasks

        # Allow override via config setting 'worker_timeout_seconds'
        cfg = load_config()
        hb_timeout = int(cfg.get('settings', {}).get('worker_timeout_seconds', HEARTBEAT_TIMEOUT))

        for worker, last_heartbeat in list(job_data.worker_status.items()):
            age = current_time - last_heartbeat
            debug_log(f"Timeout check: worker={worker} age={age:.1f}s threshold={hb_timeout}s")
            if age > hb_timeout:
                # Busy-only grace policy: require positive signal from worker (/prompt)
                # We also log assignment state for diagnostics but do not grace on it alone.
                assigned = job_data.assigned_to_workers.get(worker, [])
                incomplete_assigned = 0
                try:
                    if assigned:
                        batched_static = bool(job_data.batched_static)
                        if batched_static:
                            num_tiles_per_image = int(job_data.num_tiles_per_image or 1)
                            batch_size = int(job_data.batch_size or 1)
                            for task_id in assigned:
                                for b in range(batch_size):
                                    gidx = b * num_tiles_per_image + task_id
                                    if gidx not in completed_tasks:
                                        incomplete_assigned += 1
                                        break
                        else:
                            for task_id in assigned:
                                if task_id not in completed_tasks:
                                    incomplete_assigned += 1
                    debug_log(f"Assigned diagnostics: total_assigned={len(assigned)} incomplete_assigned={incomplete_assigned}")
                except Exception as e:
                    debug_log(f"Assigned diagnostics failed for worker {worker}: {e}")

                busy = False
                probe_queue = None
                try:
                    cfg_workers = load_config().get('workers', [])
                    wrec = next((w for w in cfg_workers if str(w.get('id')) == str(worker)), None)
                    if wrec:
                        worker_url = build_worker_url(wrec)
                        debug_log(f"Probing worker {worker} at {worker_url}/prompt")
                        payload = await probe_worker(worker_url, timeout=2.0)
                        if payload is not None:
                            probe_queue = int(payload.get('exec_info', {}).get('queue_remaining', 0))
                            busy = probe_queue is not None and probe_queue > 0
                except Exception as e:
                    debug_log(f"Probe failed for worker {worker}: {e}")
                finally:
                    debug_log(
                        f"Probe diagnostics: online={probe_queue is not None} queue_remaining={probe_queue}"
                    )

                if busy:
                    job_data.worker_status[worker] = current_time
                    debug_log(f"Heartbeat grace: worker {worker} busy via probe; skipping requeue")
                    continue

                log(f"Worker {worker} heartbeat timed out after {age:.1f}s")
                for task_id in job_data.assigned_to_workers.get(worker, []):
                    # If batched_static, task_id is a tile_idx; consider it complete only if
                    # all corresponding global_idx entries are present in completed_tasks.
                    batched_static = bool(job_data.batched_static)
                    if batched_static:
                        num_tiles_per_image = int(job_data.num_tiles_per_image or 1)
                        batch_size = int(job_data.batch_size or 1)
                        # Check all global indices for this tile across the batch
                        all_done = True
                        for b in range(batch_size):
                            gidx = b * num_tiles_per_image + task_id
                            if gidx not in completed_tasks:
                                all_done = False
                                break
                        if not all_done:
                            await job_data.pending_tasks.put(task_id)
                            requeued_count += 1
                    else:
                        if task_id not in completed_tasks:
                            await job_data.pending_tasks.put(task_id)
                            requeued_count += 1
                job_data.worker_status.pop(worker, None)
                if worker in job_data.assigned_to_workers:
                    job_data.assigned_to_workers[worker] = []

        return requeued_count

async def _get_completed_count(multi_job_id):
    """Get count of completed tasks."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
        if isinstance(job_data, BaseJobState):
            return len(job_data.completed_tasks)
        return 0

async def _mark_task_completed(multi_job_id, task_id, result):
    """Mark a task as completed."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
        if isinstance(job_data, BaseJobState):
            job_data.completed_tasks[task_id] = result

async def _send_heartbeat_to_master(multi_job_id, master_url, worker_id):
    """Send heartbeat to master."""
    try:
        data = {'multi_job_id': multi_job_id, 'worker_id': str(worker_id)}
        session = await get_client_session()
        url = f"{master_url}/distributed/heartbeat"
        async with session.post(url, json=data) as response:
            response.raise_for_status()
    except Exception as e:
        debug_log(f"Heartbeat failed: {e}")

async def _cleanup_job(multi_job_id):
    """Cleanup the job data."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        if multi_job_id in prompt_server.distributed_pending_tile_jobs:
            del prompt_server.distributed_pending_tile_jobs[multi_job_id]
            debug_log(f"Cleaned up job {multi_job_id}")

# API Endpoints (generalized)

@server.PromptServer.instance.routes.post("/distributed/heartbeat")
async def heartbeat_endpoint(request):
    try:
        data = await request.json()
        worker_id = data.get('worker_id')
        multi_job_id = data.get('multi_job_id')
        
        if not worker_id or not multi_job_id:
            return await handle_api_error(request, "Missing worker_id or multi_job_id", 400)
        
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                if isinstance(job_data, BaseJobState):
                    job_data.worker_status[worker_id] = time.time()
                    debug_log(f"Heartbeat from worker {worker_id}")
                    return web.json_response({"status": "success"})
                else:
                    return await handle_api_error(request, "Worker status tracking not available", 400)
            else:
                return await handle_api_error(request, "Job not found", 404)
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.post("/distributed/submit_tiles")
async def submit_tiles_endpoint(request):
    """Endpoint for workers to submit processed tiles in static mode."""
    try:
        content_length = request.headers.get('content-length')
        if content_length and int(content_length) > MAX_PAYLOAD_SIZE:
            return await handle_api_error(request, f"Payload too large: {content_length} bytes", 413)
        
        data = await request.post()
        multi_job_id = data.get('multi_job_id')
        worker_id = data.get('worker_id')
        is_last = data.get('is_last', 'False').lower() == 'true'
        
        if multi_job_id is None or worker_id is None:
            return await handle_api_error(request, "Missing multi_job_id or worker_id", 400)

        prompt_server = ensure_tile_jobs_initialized()
        
        batch_size = int(data.get('batch_size', 0))
        tiles = []
        
        # Handle completion signal
        if batch_size == 0 and is_last:
            async with prompt_server.distributed_tile_jobs_lock:
                if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                    job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                    if not isinstance(job_data, TileJobState):
                        return await handle_api_error(request, "Job not configured for tile submissions", 400)
                    await job_data.queue.put({
                        'worker_id': worker_id,
                        'is_last': True,
                        'tiles': []
                    })
                    debug_log(f"Received completion signal from worker {worker_id}")
                    return web.json_response({"status": "success"})
        
        try:
            tiles = _parse_tiles_from_form(data)
        except ValueError as e:
            return await handle_api_error(request, str(e), 400)

        # Submit tiles to queue
        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                if not isinstance(job_data, TileJobState):
                    return await handle_api_error(request, "Job not configured for tile submissions", 400)

                q = job_data.queue
                if batch_size > 0 or len(tiles) > 0:
                    await q.put({
                        'worker_id': worker_id,
                        'tiles': tiles,
                        'is_last': is_last
                    })
                    debug_log(f"Received {len(tiles)} tiles from worker {worker_id} (is_last={is_last})")
                else:
                    await q.put({
                        'worker_id': worker_id,
                        'is_last': True,
                        'tiles': []
                    })
                    
                return web.json_response({"status": "success"})
            else:
                return await handle_api_error(request, "Job not found", 404)
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.post("/distributed/submit_image")
async def submit_image_endpoint(request):
    """Endpoint for workers to submit processed images in dynamic mode."""
    try:
        content_length = request.headers.get('content-length')
        if content_length and int(content_length) > MAX_PAYLOAD_SIZE:
            return await handle_api_error(request, f"Payload too large: {content_length} bytes", 413)
        
        data = await request.post()
        multi_job_id = data.get('multi_job_id')
        worker_id = data.get('worker_id')
        is_last = data.get('is_last', 'False').lower() == 'true'
        
        if multi_job_id is None or worker_id is None:
            return await handle_api_error(request, "Missing multi_job_id or worker_id", 400)

        prompt_server = ensure_tile_jobs_initialized()
        
        # Handle image submission
        if 'full_image' in data and 'image_idx' in data:
            image_idx = int(data.get('image_idx'))
            img_data = data['full_image'].file.read()
            img = Image.open(io.BytesIO(img_data)).convert("RGB")
            
            debug_log(f"Received full image {image_idx} from worker {worker_id}")
            
            async with prompt_server.distributed_tile_jobs_lock:
                if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                    job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                    if not isinstance(job_data, ImageJobState):
                        return await handle_api_error(request, "Job not configured for image submissions", 400)
                    await job_data.queue.put({
                        'worker_id': worker_id,
                        'image_idx': image_idx,
                        'image': img,
                        'is_last': is_last
                    })
                    return web.json_response({"status": "success"})
        
        # Handle completion signal (no image data)
        elif is_last:
            async with prompt_server.distributed_tile_jobs_lock:
                if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                    job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                    if not isinstance(job_data, ImageJobState):
                        return await handle_api_error(request, "Job not configured for image submissions", 400)
                    await job_data.queue.put({
                        'worker_id': worker_id,
                        'is_last': True,
                    })
                    debug_log(f"Received completion signal from worker {worker_id}")
                    return web.json_response({"status": "success"})
        else:
            return await handle_api_error(request, "Missing image data or invalid request", 400)
            
        return await handle_api_error(request, "Job not found", 404)
    except Exception as e:
        return await handle_api_error(request, e, 500)

# Helper functions for shallow copying conditioning without duplicating models
def clone_control_chain(control, clone_hint=True):
    """Shallow copy the ControlNet chain, optionally cloning hints but sharing models."""
    if control is None:
        return None
    new_control = copy.copy(control)  # Shallow copy (shares model)
    if clone_hint and hasattr(control, 'cond_hint_original'):
        hint = getattr(control, 'cond_hint_original', None)
        new_control.cond_hint_original = hint.clone() if hint is not None else None
    if hasattr(control, 'previous_controlnet'):
        new_control.previous_controlnet = clone_control_chain(control.previous_controlnet, clone_hint)
    return new_control

def clone_conditioning(cond_list, clone_hints=True):
    """Clone conditioning without duplicating ControlNet models."""
    new_cond = []
    for emb, cond_dict in cond_list:
        new_emb = emb.clone() if emb is not None else None
        new_dict = cond_dict.copy()
        if 'control' in new_dict:
            new_dict['control'] = clone_control_chain(new_dict['control'], clone_hints)
        if 'mask' in new_dict:
            if new_dict['mask'] is not None:
                new_dict['mask'] = new_dict['mask'].clone()
        # Handle other potential fields if needed
        if 'pooled_output' in new_dict:
            if new_dict['pooled_output'] is not None:
                new_dict['pooled_output'] = new_dict['pooled_output'].clone()
        if 'area' in new_dict:
            new_dict['area'] = new_dict['area'][:]  # Shallow copy list/tuple
        new_cond.append([new_emb, new_dict])
    return new_cond

# Helper function to ensure persistent state is initialized
def ensure_tile_jobs_initialized():
    """Ensure tile job storage is initialized on the server instance."""
    prompt_server = server.PromptServer.instance
    if not hasattr(prompt_server, 'distributed_pending_tile_jobs'):
        debug_log("Initializing persistent tile job queue on server instance.")
        prompt_server.distributed_pending_tile_jobs = {}
        prompt_server.distributed_tile_jobs_lock = asyncio.Lock()
    else:
        invalid_job_ids = [
            job_id
            for job_id, job_data in prompt_server.distributed_pending_tile_jobs.items()
            if not isinstance(job_data, BaseJobState)
        ]
        for job_id in invalid_job_ids:
            debug_log(f"Removing invalid job state for {job_id}")
            del prompt_server.distributed_pending_tile_jobs[job_id]
    return prompt_server

# API Endpoint for tile completion
@server.PromptServer.instance.routes.post("/distributed/request_image")
async def request_image_endpoint(request):
    """Endpoint for workers to request tasks (images in dynamic mode, tiles in static mode)."""
    try:
        data = await request.json()
        worker_id = data.get('worker_id')
        multi_job_id = data.get('multi_job_id')
        
        if not worker_id or not multi_job_id:
            return await handle_api_error(request, "Missing worker_id or multi_job_id", 400)
        
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                if not isinstance(job_data, BaseJobState):
                    return await handle_api_error(request, "Invalid job data structure", 500)

                mode = job_data.mode
                if isinstance(job_data, ImageJobState):
                    pending_queue = job_data.pending_images
                elif isinstance(job_data, TileJobState):
                    pending_queue = job_data.pending_tasks
                else:
                    return await handle_api_error(request, "Invalid job configuration", 400)
                
                try:
                    task_idx = await asyncio.wait_for(pending_queue.get(), timeout=0.1)
                    # Track assigned task
                    job_data.assigned_to_workers.setdefault(worker_id, []).append(task_idx)
                    # Update worker heartbeat
                    job_data.worker_status[worker_id] = time.time()
                    # Get estimated remaining count
                    remaining = pending_queue.qsize()  # Approximate
                    
                    # Return appropriate response based on mode
                    if mode == 'dynamic':
                        debug_log(f"UltimateSDUpscale API - Assigned image {task_idx} to worker {worker_id}")
                        return web.json_response({"image_idx": task_idx, "estimated_remaining": remaining})
                    else:  # static
                        debug_log(f"UltimateSDUpscale API - Assigned tile {task_idx} to worker {worker_id}")
                        return web.json_response({"tile_idx": task_idx, "estimated_remaining": remaining, "batched_static": job_data.batched_static})
                except asyncio.TimeoutError:
                    if mode == 'dynamic':
                        return web.json_response({"image_idx": None})
                    else:
                        return web.json_response({"tile_idx": None})
            else:
                return await handle_api_error(request, "Job not found", 404)
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.get("/distributed/job_status")
async def job_status_endpoint(request):
    """Endpoint to check if a job is ready."""
    multi_job_id = request.query.get('multi_job_id')
    if not multi_job_id:
        return web.json_response({"ready": False})
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
        ready = bool(isinstance(job_data, BaseJobState) and job_data.queue is not None)
        return web.json_response({"ready": ready})
