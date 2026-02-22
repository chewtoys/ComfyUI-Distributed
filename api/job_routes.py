import json
import asyncio
import io
import uuid
import time
import os

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
from ..distributed_queue_api import orchestrate_distributed_execution

prompt_server = _server.PromptServer.instance


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

    prompt = data.get("prompt")
    if not isinstance(prompt, dict):
        return await handle_api_error(request, "Field 'prompt' must be an object", 400)

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
        
        import folder_paths
        import base64
        import io
        import hashlib
        
        # Use ComfyUI's folder paths to find the file
        full_path = folder_paths.get_annotated_filepath(image_path)
        
        if not os.path.exists(full_path):
            return await handle_api_error(request, f"File not found: {image_path}", 404)
        
        # Calculate file hash
        hash_md5 = hashlib.md5()
        with open(full_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        file_hash = hash_md5.hexdigest()
        
        # Check if it's a video file
        video_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.webm'}
        file_ext = os.path.splitext(full_path)[1].lower()
        
        if file_ext in video_extensions:
            # For video files, read the raw bytes
            with open(full_path, 'rb') as f:
                file_data = f.read()
            
            # Determine MIME type
            mime_types = {
                '.mp4': 'video/mp4',
                '.avi': 'video/x-msvideo', 
                '.mov': 'video/quicktime',
                '.mkv': 'video/x-matroska',
                '.webm': 'video/webm'
            }
            mime_type = mime_types.get(file_ext, 'video/mp4')
            
            # Return base64 encoded video with data URL
            video_base64 = base64.b64encode(file_data).decode('utf-8')
            return web.json_response({
                "status": "success",
                "image_data": f"data:{mime_type};base64,{video_base64}",
                "hash": file_hash
            })
        else:
            # For images, use PIL
            from PIL import Image
            
            # Load and convert to base64
            with Image.open(full_path) as img:
                # Convert to RGB if needed
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGB')
                
                # Save to bytes
                buffer = io.BytesIO()
                img.save(buffer, format='PNG', compress_level=1)  # Fast compression
                img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
            return web.json_response({
                "status": "success",
                "image_data": f"data:image/png;base64,{img_base64}",
                "hash": file_hash
            })
        
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
        
        import folder_paths
        import hashlib
        
        # Use ComfyUI's folder paths to find the file
        full_path = folder_paths.get_annotated_filepath(filename)
        
        if not os.path.exists(full_path):
            return web.json_response({
                "status": "success",
                "exists": False
            })
        
        # Calculate file hash
        hash_md5 = hashlib.md5()
        with open(full_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        file_hash = hash_md5.hexdigest()
        
        # Check if hash matches
        hash_matches = file_hash == expected_hash
        
        return web.json_response({
            "status": "success",
            "exists": True,
            "hash_matches": hash_matches
        })
        
    except Exception as e:
        return await handle_api_error(request, e, 500)


@server.PromptServer.instance.routes.post("/distributed/job_complete")
async def job_complete_endpoint(request):
    try:
        data = await request.post()
        multi_job_id = data.get('multi_job_id')
        worker_id = data.get('worker_id')
        is_last = data.get('is_last', 'False').lower() == 'true'
        
        if multi_job_id is None or worker_id is None:
            return await handle_api_error(request, "Missing multi_job_id or worker_id", 400)

        # Check for batch mode
        batch_size = int(data.get('batch_size', 0))
        tensors = []
        
        if batch_size > 0:
            # Batch mode: Extract multiple images
            debug_log(f"job_complete received batch - job_id: {multi_job_id}, worker: {worker_id}, batch_size: {batch_size}")
            
            # Check for JSON metadata
            metadata_field = data.get('images_metadata')
            metadata = None
            if metadata_field:
                # New JSON metadata format
                try:
                    # Handle different types of metadata field
                    if hasattr(metadata_field, 'file'):
                        # File-like object
                        metadata_str = metadata_field.file.read().decode('utf-8')
                    elif isinstance(metadata_field, (bytes, bytearray)):
                        # Direct bytes/bytearray
                        metadata_str = metadata_field.decode('utf-8')
                    else:
                        # String
                        metadata_str = str(metadata_field)
                    
                    metadata = json.loads(metadata_str)
                    if len(metadata) != batch_size:
                        return await handle_api_error(request, "Metadata length mismatch", 400)
                    debug_log(f"Using JSON metadata for batch from worker {worker_id}")
                except Exception as e:
                    log(f"Error parsing JSON metadata from worker {worker_id}: {e}")
                    return await handle_api_error(request, f"Metadata parsing error: {e}", 400)
            else:
                # Legacy format - log deprecation warning
                debug_log(f"WARNING: Worker {worker_id} using legacy field format. Please update to use JSON metadata.")
            
            # Process images with per-item error handling
            image_data_list = []  # List of tuples: (index, tensor)
            
            for i in range(batch_size):
                image_field = data.get(f'image_{i}')
                if image_field is None:
                    log(f"Missing image_{i} from worker {worker_id}, skipping")
                    continue
                    
                try:
                    img_data = image_field.file.read()
                    img = Image.open(io.BytesIO(img_data)).convert("RGB")
                    tensor = pil_to_tensor(img)
                    tensor = ensure_contiguous(tensor)
                    
                    # Get expected index from metadata or use position
                    if metadata and i < len(metadata):
                        expected_idx = metadata[i].get('index', i)
                        if i != expected_idx:
                            debug_log(f"Warning: Image order mismatch at position {i}, expected index {expected_idx}")
                    else:
                        expected_idx = i
                    
                    image_data_list.append((expected_idx, tensor))
                except Exception as e:
                    log(f"Error processing image {i} from worker {worker_id}: {e}, skipping this image")
                    # Continue processing other images instead of failing entire batch
                    continue
            
            # Check if we got any valid images
            if not image_data_list:
                return await handle_api_error(request, "No valid images in batch", 400)
            
            # Sort by index to ensure correct order
            image_data_list.sort(key=lambda x: x[0])
            tensors = [tensor for _, tensor in image_data_list]
            
            # Keep the indices for later use
            indices = [idx for idx, _ in image_data_list]
            
            # Validate final order
            if metadata:
                debug_log(f"Reordered {len(tensors)} images based on metadata indices")
        else:
            # Fallback for single-image mode (backward compatibility)
            image_file = data.get('image')
            image_index = data.get('image_index')
            
            debug_log(f"job_complete received single - job_id: {multi_job_id}, worker: {worker_id}, index: {image_index}, is_last: {is_last}")
            
            if not image_file:
                return await handle_api_error(request, "Missing image data", 400)
                
            try:
                img_data = image_file.file.read()
                img = Image.open(io.BytesIO(img_data)).convert("RGB")
                tensor = pil_to_tensor(img)
                tensor = ensure_contiguous(tensor)
                tensors = [tensor]
            except Exception as e:
                log(f"Error processing image from worker {worker_id}: {e}")
                return await handle_api_error(request, f"Image processing error: {e}", 400)

        # Parse audio data if present
        audio_data = None
        audio_waveform_field = data.get('audio_waveform')
        audio_sample_rate = data.get('audio_sample_rate')
        if audio_waveform_field is not None:
            try:
                waveform_bytes = audio_waveform_field.file.read()
                waveform_buffer = io.BytesIO(waveform_bytes)
                try:
                    waveform_tensor = torch.load(waveform_buffer, weights_only=True)
                except TypeError:
                    waveform_tensor = torch.load(waveform_buffer)
                sample_rate = int(audio_sample_rate) if audio_sample_rate else 44100
                audio_data = {"waveform": waveform_tensor, "sample_rate": sample_rate}
                debug_log(f"Received audio from worker {worker_id}: shape={waveform_tensor.shape}, sample_rate={sample_rate}")
            except Exception as e:
                log(f"Error parsing audio from worker {worker_id}: {e}")

        # Put batch into queue
        async with prompt_server.distributed_jobs_lock:
            debug_log(f"Current pending jobs: {list(prompt_server.distributed_pending_jobs.keys())}")
            if multi_job_id in prompt_server.distributed_pending_jobs:
                if batch_size > 0:
                    # Put batch as single item with indices
                    await prompt_server.distributed_pending_jobs[multi_job_id].put({
                        'worker_id': worker_id,
                        'tensors': tensors,
                        'indices': indices,
                        'is_last': is_last,
                        'audio': audio_data
                    })
                    debug_log(f"Received batch result for job {multi_job_id} from worker {worker_id}, size={len(tensors)}")
                else:
                    # Put single image (backward compat)
                    await prompt_server.distributed_pending_jobs[multi_job_id].put({
                        'tensor': tensors[0],
                        'worker_id': worker_id,
                        'image_index': int(image_index) if image_index else 0,
                        'is_last': is_last,
                        'audio': audio_data
                    })
                    debug_log(f"Received single result for job {multi_job_id} from worker {worker_id}")
                    
                debug_log(f"Queue size after put: {prompt_server.distributed_pending_jobs[multi_job_id].qsize()}")
                return web.json_response({"status": "success"})
            else:
                log(f"ERROR: Job {multi_job_id} not found in distributed_pending_jobs")
                return await handle_api_error(request, "Job not found or already complete", 404)
    except Exception as e:
        return await handle_api_error(request, e)
