import json
import asyncio
import io
import os
import base64
import binascii

from aiohttp import web
import server
import server as _server
import torch
from PIL import Image

from ..utils.logging import debug_log, log
from ..utils.image import pil_to_tensor, ensure_contiguous
from ..utils.network import handle_api_error
from ..utils.constants import MEMORY_CLEAR_DELAY
from ..utils.async_helpers import queue_prompt_payload
from .queue_orchestration import orchestrate_distributed_execution

prompt_server = _server.PromptServer.instance

# Canonical worker result envelope accepted by POST /distributed/job_complete:
# { "job_id": str, "worker_id": str, "batch_idx": int, "image": <base64 PNG>, "is_last": bool }


def _decode_image_sync(image_path):
    """Decode image/video file and compute hash in a threadpool worker."""
    import base64
    import hashlib
    import folder_paths

    full_path = folder_paths.get_annotated_filepath(image_path)
    if not os.path.exists(full_path):
        raise FileNotFoundError(image_path)

    hash_md5 = hashlib.md5()
    with open(full_path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    file_hash = hash_md5.hexdigest()

    video_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.webm'}
    file_ext = os.path.splitext(full_path)[1].lower()

    if file_ext in video_extensions:
        with open(full_path, 'rb') as f:
            file_data = f.read()
        mime_types = {
            '.mp4': 'video/mp4',
            '.avi': 'video/x-msvideo',
            '.mov': 'video/quicktime',
            '.mkv': 'video/x-matroska',
            '.webm': 'video/webm'
        }
        mime_type = mime_types.get(file_ext, 'video/mp4')
        image_data = f"data:{mime_type};base64,{base64.b64encode(file_data).decode('utf-8')}"
    else:
        with Image.open(full_path) as img:
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')
            buffer = io.BytesIO()
            img.save(buffer, format='PNG', compress_level=1)
            image_data = f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('utf-8')}"

    return {
        "status": "success",
        "image_data": image_data,
        "hash": file_hash,
    }


def _check_file_sync(filename, expected_hash):
    """Check file presence and hash in a threadpool worker."""
    import hashlib
    import folder_paths

    full_path = folder_paths.get_annotated_filepath(filename)
    if not os.path.exists(full_path):
        return {
            "status": "success",
            "exists": False,
        }

    hash_md5 = hashlib.md5()
    with open(full_path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    file_hash = hash_md5.hexdigest()

    return {
        "status": "success",
        "exists": True,
        "hash_matches": file_hash == expected_hash,
    }


def _decode_canonical_png_tensor(image_payload):
    """Decode canonical base64 PNG payload into a contiguous IMAGE tensor."""
    if not isinstance(image_payload, str) or not image_payload.strip():
        raise ValueError("Field 'image' must be a non-empty base64 PNG string.")

    encoded = image_payload.strip()
    if encoded.startswith("data:"):
        header, sep, data_part = encoded.partition(",")
        if not sep:
            raise ValueError("Field 'image' data URL is malformed.")
        if not header.lower().startswith("data:image/png;base64"):
            raise ValueError("Field 'image' must be a PNG data URL when using data:* format.")
        encoded = data_part

    try:
        png_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Field 'image' is not valid base64 PNG data.") from exc

    if not png_bytes:
        raise ValueError("Field 'image' decoded to empty PNG data.")

    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            img = img.convert("RGB")
            tensor = pil_to_tensor(img)
        return ensure_contiguous(tensor)
    except Exception as exc:
        raise ValueError(f"Failed to decode PNG image payload: {exc}") from exc


@server.PromptServer.instance.routes.post("/distributed/prepare_job")
async def prepare_job_endpoint(request):
    try:
        data = await request.json()
        multi_job_id = data.get('multi_job_id')
        if not multi_job_id:
            return await handle_api_error(request, "Missing multi_job_id", 400)

        async with prompt_server.distributed_jobs_lock:
            if multi_job_id not in prompt_server.distributed_pending_jobs:
                prompt_server.distributed_pending_jobs[multi_job_id] = asyncio.Queue()
        
        debug_log(f"Prepared queue for job {multi_job_id}")
        return web.json_response({"status": "success"})
    except Exception as e:
        return await handle_api_error(request, e)

@server.PromptServer.instance.routes.post("/distributed/clear_memory")
async def clear_memory_endpoint(request):
    debug_log("Received request to clear VRAM.")
    try:
        # Use ComfyUI's prompt server queue system like the /free endpoint does
        if hasattr(server.PromptServer.instance, 'prompt_queue'):
            server.PromptServer.instance.prompt_queue.set_flag("unload_models", True)
            server.PromptServer.instance.prompt_queue.set_flag("free_memory", True)
            debug_log("Set queue flags for memory clearing.")
        
        # Wait a bit for the queue to process
        await asyncio.sleep(MEMORY_CLEAR_DELAY)
        
        # Also do direct cleanup as backup, but with error handling
        import gc
        import comfy.model_management as mm
        
        try:
            mm.unload_all_models()
        except AttributeError as e:
            debug_log(f"Warning during model unload: {e}")
        
        try:
            mm.soft_empty_cache()
        except Exception as e:
            debug_log(f"Warning during cache clear: {e}")
        
        for _ in range(3):
            gc.collect()
        
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        
        debug_log("VRAM cleared successfully.")
        return web.json_response({"status": "success", "message": "GPU memory cleared."})
    except Exception as e:
        # Even if there's an error, try to do basic cleanup
        import gc
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        debug_log(f"Partial VRAM clear completed with warning: {e}")
        return web.json_response({"status": "success", "message": "GPU memory cleared (with warnings)"})


@server.PromptServer.instance.routes.post("/distributed/queue")
async def distributed_queue_endpoint(request):
    """Queue a distributed workflow, mirroring the UI orchestration pipeline."""
    try:
        data = await request.json()
    except Exception as exc:
        return await handle_api_error(request, f"Invalid JSON payload: {exc}", 400)

    auto_prepare = bool(data.get("auto_prepare"))
    prompt = data.get("prompt")
    if prompt is None and auto_prepare:
        # Transitional compatibility: allow payload.workflow.prompt when auto_prepare is enabled.
        workflow_payload = data.get("workflow")
        if isinstance(workflow_payload, dict):
            candidate_prompt = workflow_payload.get("prompt")
            if isinstance(candidate_prompt, dict):
                prompt = candidate_prompt

    if not isinstance(prompt, dict):
        return await handle_api_error(request, "Field 'prompt' must be an object", 400)

    # Accept either enabled_worker_ids (legacy) or workers list (auto-prepare path).
    workers_field = data.get("workers")
    if workers_field is not None and data.get("enabled_worker_ids") is None:
        if isinstance(workers_field, list):
            enabled_ids = []
            for entry in workers_field:
                if isinstance(entry, dict):
                    worker_id = entry.get("id")
                else:
                    worker_id = entry
                if worker_id is not None:
                    enabled_ids.append(str(worker_id))
            data["enabled_worker_ids"] = enabled_ids

    workflow_meta = data.get("workflow")
    client_id = data.get("client_id")
    delegate_master = data.get("delegate_master")
    enabled_ids = data.get("enabled_worker_ids")

    if enabled_ids is not None:
        if not isinstance(enabled_ids, list):
            return await handle_api_error(request, "enabled_worker_ids must be a list of worker IDs", 400)
        enabled_ids = [str(worker_id) for worker_id in enabled_ids]

    try:
        prompt_id, worker_count = await orchestrate_distributed_execution(
            prompt,
            workflow_meta,
            client_id,
            enabled_worker_ids=enabled_ids,
            delegate_master=delegate_master,
        )
        return web.json_response({
            "prompt_id": prompt_id,
            "worker_count": worker_count,
            "auto_prepare_supported": True,
        })
    except Exception as exc:
        return await handle_api_error(request, exc, 500)

@server.PromptServer.instance.routes.post("/distributed/load_image")
async def load_image_endpoint(request):
    """Load an image or video file and return it as base64 data with hash."""
    try:
        data = await request.json()
        image_path = data.get("image_path")
        
        if not image_path:
            return await handle_api_error(request, "Missing image_path", 400)
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(None, _decode_image_sync, image_path)
        return web.json_response(payload)
    except FileNotFoundError:
        return await handle_api_error(request, f"File not found: {image_path}", 404)
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.post("/distributed/check_file")
async def check_file_endpoint(request):
    """Check if a file exists and matches the given hash."""
    try:
        data = await request.json()
        filename = data.get("filename")
        expected_hash = data.get("hash")
        
        if not filename or not expected_hash:
            return await handle_api_error(request, "Missing filename or hash", 400)
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(None, _check_file_sync, filename, expected_hash)
        return web.json_response(payload)
        
    except Exception as e:
        return await handle_api_error(request, e, 500)


@server.PromptServer.instance.routes.post("/distributed/job_complete")
async def job_complete_endpoint(request):
    try:
        data = await request.json()
    except Exception as exc:
        return await handle_api_error(request, f"Invalid JSON payload: {exc}", 400)

    if not isinstance(data, dict):
        return await handle_api_error(request, "Expected a JSON object body", 400)

    try:
        job_id = data.get("job_id")
        worker_id = data.get("worker_id")
        batch_idx = data.get("batch_idx")
        image_payload = data.get("image")
        is_last = data.get("is_last")

        errors = []
        if not isinstance(job_id, str) or not job_id.strip():
            errors.append("job_id: expected non-empty string")
        if not isinstance(worker_id, str) or not worker_id.strip():
            errors.append("worker_id: expected non-empty string")
        if not isinstance(batch_idx, int) or batch_idx < 0:
            errors.append("batch_idx: expected non-negative integer")
        if not isinstance(image_payload, str) or not image_payload.strip():
            errors.append("image: expected non-empty base64 PNG string")
        if not isinstance(is_last, bool):
            errors.append("is_last: expected boolean")
        if errors:
            return web.json_response({"error": errors}, status=400)

        tensor = _decode_canonical_png_tensor(image_payload)
        multi_job_id = job_id.strip()
        worker_id = worker_id.strip()

        async with prompt_server.distributed_jobs_lock:
            pending = prompt_server.distributed_pending_jobs.get(multi_job_id)
            if pending is None:
                log(f"ERROR: Job {multi_job_id} not found in distributed_pending_jobs")
                return await handle_api_error(request, "Job not found or already complete", 404)

            await pending.put(
                {
                    "tensor": tensor,
                    "worker_id": worker_id,
                    "image_index": int(batch_idx),
                    "is_last": is_last,
                    "audio": None,
                }
            )
            debug_log(
                f"job_complete received canonical envelope - job_id: {multi_job_id}, "
                f"worker: {worker_id}, batch_idx: {batch_idx}, is_last: {is_last}, "
                f"queue_size: {pending.qsize()}"
            )

        return web.json_response({"status": "success"})
    except Exception as e:
        return await handle_api_error(request, e)
