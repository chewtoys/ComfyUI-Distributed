import asyncio
import io
import time

from aiohttp import web
from PIL import Image
import server

from ..upscale.job_models import BaseJobState, ImageJobState, TileJobState
from ..upscale.job_store import MAX_PAYLOAD_SIZE, ensure_tile_jobs_initialized
from ..upscale.payload_parsers import _parse_tiles_from_form
from ..utils.logging import debug_log
from ..utils.network import handle_api_error


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
                return await handle_api_error(request, "Worker status tracking not available", 400)
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
                        'tiles': [],
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
                        'is_last': is_last,
                    })
                    debug_log(f"Received {len(tiles)} tiles from worker {worker_id} (is_last={is_last})")
                else:
                    await q.put({
                        'worker_id': worker_id,
                        'is_last': True,
                        'tiles': [],
                    })

                return web.json_response({"status": "success"})
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
                        'is_last': is_last,
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
                    job_data.assigned_to_workers.setdefault(worker_id, []).append(task_idx)
                    job_data.worker_status[worker_id] = time.time()
                    remaining = pending_queue.qsize()

                    if mode == 'dynamic':
                        debug_log(f"UltimateSDUpscale API - Assigned image {task_idx} to worker {worker_id}")
                        return web.json_response({"image_idx": task_idx, "estimated_remaining": remaining})
                    debug_log(f"UltimateSDUpscale API - Assigned tile {task_idx} to worker {worker_id}")
                    return web.json_response({
                        "tile_idx": task_idx,
                        "estimated_remaining": remaining,
                        "batched_static": job_data.batched_static,
                    })
                except asyncio.TimeoutError:
                    if mode == 'dynamic':
                        return web.json_response({"image_idx": None})
                    return web.json_response({"tile_idx": None})
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
