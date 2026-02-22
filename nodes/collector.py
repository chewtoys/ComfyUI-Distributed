import torch
import numpy as np
import io
import json
import asyncio
import time

import aiohttp
import server as _server
import comfy.model_management
from comfy.utils import ProgressBar

from ..utils.logging import debug_log, log
from ..utils.config import get_worker_timeout_seconds, load_config, is_master_delegate_only
from ..utils.image import tensor_to_pil, pil_to_tensor, ensure_contiguous
from ..utils.network import get_client_session, normalize_host
from ..utils.async_helpers import run_async_in_server_loop
from ..utils.constants import MAX_BATCH
from ..workers.detection import is_local_worker, get_comms_channel

prompt_server = _server.PromptServer.instance


class DistributedCollectorNode:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": { "images": ("IMAGE",) },
            "optional": { "audio": ("AUDIO",) },
            "hidden": {
                "multi_job_id": ("STRING", {"default": ""}),
                "is_worker": ("BOOLEAN", {"default": False}),
                "master_url": ("STRING", {"default": ""}),
                "enabled_worker_ids": ("STRING", {"default": "[]"}),
                "worker_batch_size": ("INT", {"default": 1, "min": 1, "max": 1024}),
                "worker_id": ("STRING", {"default": ""}),
                "pass_through": ("BOOLEAN", {"default": False}),
                "delegate_only": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    FUNCTION = "run"
    CATEGORY = "image"
    
    def run(self, images, audio=None, multi_job_id="", is_worker=False, master_url="", enabled_worker_ids="[]", worker_batch_size=1, worker_id="", pass_through=False, delegate_only=False):
        # Create empty audio if not provided
        empty_audio = {"waveform": torch.zeros(1, 2, 1), "sample_rate": 44100}

        if not multi_job_id or pass_through:
            if pass_through:
                print(f"[Distributed Collector] Pass-through mode enabled, returning images unchanged")
            return (images, audio if audio is not None else empty_audio)

        # Use async helper to run in server loop
        result = run_async_in_server_loop(
            self.execute(images, audio, multi_job_id, is_worker, master_url, enabled_worker_ids, worker_batch_size, worker_id, delegate_only)
        )
        return result

    async def send_batch_to_master(self, image_batch, audio, multi_job_id, master_url, worker_id):
        """Send image batch and optional audio to master, chunked if large."""
        batch_size = image_batch.shape[0]
        if batch_size == 0 and audio is None:
            return

        for start in range(0, batch_size, MAX_BATCH):
            chunk = image_batch[start:start + MAX_BATCH]
            chunk_size = chunk.shape[0]
            is_chunk_last = (start + chunk_size == batch_size)  # True only for final chunk

            data = aiohttp.FormData()
            data.add_field('multi_job_id', multi_job_id)
            data.add_field('worker_id', str(worker_id))
            data.add_field('is_last', str(is_chunk_last))
            data.add_field('batch_size', str(chunk_size))

            # Chunk metadata: Absolute index from full batch
            metadata = [{'index': start + j} for j in range(chunk_size)]
            data.add_field('images_metadata', json.dumps(metadata), content_type='application/json')

            # Add chunk images
            for j in range(chunk_size):
                # Convert tensor slice to PIL
                img = tensor_to_pil(chunk[j:j+1], 0)
                byte_io = io.BytesIO()
                img.save(byte_io, format='PNG', compress_level=0)
                byte_io.seek(0)
                data.add_field(f'image_{j}', byte_io, filename=f'image_{j}.png', content_type='image/png')

            # Add audio data only on the final chunk to avoid duplication
            if is_chunk_last and audio is not None:
                waveform = audio.get("waveform")
                sample_rate = audio.get("sample_rate", 44100)
                if waveform is not None and waveform.numel() > 0:
                    # Serialize waveform tensor to bytes
                    audio_bytes = io.BytesIO()
                    torch.save(waveform, audio_bytes)
                    audio_bytes.seek(0)
                    data.add_field('audio_waveform', audio_bytes, filename='audio.pt', content_type='application/octet-stream')
                    data.add_field('audio_sample_rate', str(sample_rate))
                    debug_log(f"Worker - Including audio: shape={waveform.shape}, sample_rate={sample_rate}")

            try:
                session = await get_client_session()
                url = f"{master_url}/distributed/job_complete"
                async with session.post(url, data=data) as response:
                    response.raise_for_status()
            except Exception as e:
                log(f"Worker - Failed to send chunk to master: {e}")
                debug_log(f"Worker - Full error details: URL={url}")
                raise  # Re-raise to handle at caller level

    def _combine_audio(self, master_audio, worker_audio, empty_audio):
        """Combine audio from master and workers into a single audio output."""
        audio_pieces = []
        sample_rate = 44100

        # Add master audio first if present
        if master_audio is not None:
            waveform = master_audio.get("waveform")
            if waveform is not None and waveform.numel() > 0:
                audio_pieces.append(waveform)
                sample_rate = master_audio.get("sample_rate", 44100)

        # Add worker audio in sorted order
        for worker_id_str in sorted(worker_audio.keys()):
            w_audio = worker_audio[worker_id_str]
            if w_audio is not None:
                waveform = w_audio.get("waveform")
                if waveform is not None and waveform.numel() > 0:
                    audio_pieces.append(waveform)
                    # Use first available sample rate
                    if sample_rate == 44100:
                        sample_rate = w_audio.get("sample_rate", 44100)

        if not audio_pieces:
            return empty_audio

        try:
            # Concatenate along the samples dimension (dim=-1)
            # Ensure all pieces have same batch and channel dimensions
            combined_waveform = torch.cat(audio_pieces, dim=-1)
            debug_log(f"Master - Combined audio: {len(audio_pieces)} pieces, final shape={combined_waveform.shape}")
            return {"waveform": combined_waveform, "sample_rate": sample_rate}
        except Exception as e:
            log(f"Master - Error combining audio: {e}")
            return empty_audio

    async def execute(self, images, audio, multi_job_id="", is_worker=False, master_url="", enabled_worker_ids="[]", worker_batch_size=1, worker_id="", delegate_only=False):
        empty_audio = {"waveform": torch.zeros(1, 2, 1), "sample_rate": 44100}

        if is_worker:
            # Worker mode: send images and audio to master in a single batch
            debug_log(f"Worker - Job {multi_job_id} complete. Sending {images.shape[0]} image(s) to master")
            await self.send_batch_to_master(images, audio, multi_job_id, master_url, worker_id)
            return (images, audio if audio is not None else empty_audio)
        else:
            delegate_mode = delegate_only or is_master_delegate_only()
            # Master mode: collect images and audio from workers
            enabled_workers = json.loads(enabled_worker_ids)
            num_workers = len(enabled_workers)
            if num_workers == 0:
                return (images, audio if audio is not None else empty_audio)

            if delegate_mode:
                master_batch_size = 0
                images_on_cpu = None
                master_audio = None
                debug_log(f"Master - Job {multi_job_id}: Delegate-only mode enabled, collecting exclusively from {num_workers} workers")
            else:
                images_on_cpu = images.cpu()
                master_batch_size = images.shape[0]
                master_audio = audio  # Keep master's audio for later
                debug_log(f"Master - Job {multi_job_id}: Master has {master_batch_size} images, collecting from {num_workers} workers...")

                # Ensure master images are contiguous
                images_on_cpu = ensure_contiguous(images_on_cpu)


            # Initialize storage for collected images and audio
            worker_images = {}  # Dict to store images by worker_id and index
            worker_audio = {}   # Dict to store audio by worker_id
            
            # Get the existing queue - it should already exist from prepare_job
            async with prompt_server.distributed_jobs_lock:
                if multi_job_id not in prompt_server.distributed_pending_jobs:
                    log(f"Master - WARNING: Queue doesn't exist for job {multi_job_id}, creating one")
                    prompt_server.distributed_pending_jobs[multi_job_id] = asyncio.Queue()
                else:
                    existing_size = prompt_server.distributed_pending_jobs[multi_job_id].qsize()
                    debug_log(f"Master - Using existing queue for job {multi_job_id} (current size: {existing_size})")
            
            # Collect images until all workers report they're done
            collected_count = 0
            workers_done = set()
            
            # Use unified worker timeout from config/UI with simple sliced waits
            base_timeout = float(get_worker_timeout_seconds())
            slice_timeout = min(0.5, base_timeout)  # small per-wait slice to recheck interrupt
            last_activity = time.time()
            
            
            # Get queue size before starting
            async with prompt_server.distributed_jobs_lock:
                q = prompt_server.distributed_pending_jobs[multi_job_id]
                initial_size = q.qsize()

            # NEW: Initialize progress bar for workers (total = num_workers)
            p = ProgressBar(num_workers)

            try:
                while len(workers_done) < num_workers:
                    # Check for user interruption to abort collection promptly
                    comfy.model_management.throw_exception_if_processing_interrupted()
                    try:
                        # Get the queue again each time to ensure we have the right reference
                        async with prompt_server.distributed_jobs_lock:
                            q = prompt_server.distributed_pending_jobs[multi_job_id]
                            current_size = q.qsize()
                        
                        result = await asyncio.wait_for(q.get(), timeout=slice_timeout)
                        worker_id = result['worker_id']
                        is_last = result.get('is_last', False)
                        
                        # Check if batch mode
                        tensors = result.get('tensors', [])
                        indices = result.get('indices', [])  # Get the indices
                        if tensors:
                            # Batch mode
                            debug_log(f"Master - Got batch from worker {worker_id}, size={len(tensors)}, is_last={is_last}")
                            
                            if worker_id not in worker_images:
                                worker_images[worker_id] = {}
                            
                            # Use actual indices if available, otherwise fall back to sequential
                            if indices:
                                for i, tensor in enumerate(tensors):
                                    actual_idx = indices[i]
                                    worker_images[worker_id][actual_idx] = tensor
                            else:
                                # Fallback for backward compatibility
                                for idx, tensor in enumerate(tensors):
                                    worker_images[worker_id][idx] = tensor
                            
                            collected_count += len(tensors)
                        else:
                            # Single image mode (backward compat)
                            image_index = result['image_index']
                            tensor = result['tensor']

                            debug_log(f"Master - Got single result from worker {worker_id}, image {image_index}, is_last={is_last}")

                            if worker_id not in worker_images:
                                worker_images[worker_id] = {}
                            worker_images[worker_id][image_index] = tensor

                            collected_count += 1

                        # Collect audio data if present
                        result_audio = result.get('audio')
                        if result_audio is not None:
                            worker_audio[worker_id] = result_audio
                            debug_log(f"Master - Got audio from worker {worker_id}")

                        # Record activity and refresh timeout baseline
                        last_activity = time.time()
                        base_timeout = float(get_worker_timeout_seconds())

                        if is_last:
                            workers_done.add(worker_id)
                            p.update(1)  # +1 per completed worker
                        
                    except asyncio.TimeoutError:
                        # If we still have time, continue polling; otherwise handle timeout
                        if (time.time() - last_activity) < base_timeout:
                            comfy.model_management.throw_exception_if_processing_interrupted()
                            continue
                        # Re-check for user interruption after timeout expiry
                        comfy.model_management.throw_exception_if_processing_interrupted()
                        missing_workers = set(str(w) for w in enabled_workers) - workers_done
                        log(f"Master - Timeout. Still waiting for workers: {list(missing_workers)}")

                        # Probe missing workers' /prompt endpoints to check if they are actively processing
                        any_busy = False
                        try:
                            cfg = load_config()
                            cfg_workers = cfg.get('workers', [])
                            session = await get_client_session()
                            for wid in list(missing_workers):
                                wrec = next((w for w in cfg_workers if str(w.get('id')) == str(wid)), None)
                                if not wrec:
                                    debug_log(f"Collector probe: worker {wid} not found in config")
                                    continue
                                host = normalize_host(wrec.get('host') or 'localhost') or 'localhost'
                                port = int(wrec.get('port', 8188))
                                url = f"http://{host}:{port}/prompt"
                                try:
                                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
                                        status = resp.status
                                        q = None
                                        if status == 200:
                                            try:
                                                payload = await resp.json()
                                                q = int(payload.get('exec_info', {}).get('queue_remaining', 0))
                                            except Exception:
                                                q = 0
                                        debug_log(f"Collector probe: worker {wid} status={status} queue_remaining={q}")
                                        if status == 200 and q and q > 0:
                                            any_busy = True
                                            log(f"Master - Probe grace: worker {wid} appears busy (queue_remaining={q}). Continuing to wait.")
                                            break
                                except Exception as e:
                                    debug_log(f"Collector probe failed for worker {wid}: {e}")
                        except Exception as e:
                            debug_log(f"Collector probe setup error: {e}")

                        if any_busy:
                            # Refresh last_activity and continue waiting
                            last_activity = time.time()
                            # Refresh base timeout in case the user changed it in UI
                            base_timeout = float(get_worker_timeout_seconds())
                            continue
                        
                        # Check queue size again with lock
                        async with prompt_server.distributed_jobs_lock:
                            if multi_job_id in prompt_server.distributed_pending_jobs:
                                final_q = prompt_server.distributed_pending_jobs[multi_job_id]
                                final_size = final_q.qsize()
                                
                                # Try to drain any remaining items
                                remaining_items = []
                                while not final_q.empty():
                                    try:
                                        item = final_q.get_nowait()
                                        remaining_items.append(item)
                                    except asyncio.QueueEmpty:
                                        break
                                
                                if remaining_items:
                                    # Process them
                                    for item in remaining_items:
                                        worker_id = item['worker_id']
                                        is_last = item.get('is_last', False)
                                        
                                        # Check if batch mode
                                        tensors = item.get('tensors', [])
                                        if tensors:
                                            # Batch mode
                                            if worker_id not in worker_images:
                                                worker_images[worker_id] = {}
                                            
                                            for idx, tensor in enumerate(tensors):
                                                worker_images[worker_id][idx] = tensor
                                            
                                            collected_count += len(tensors)
                                        else:
                                            # Single image mode
                                            image_index = item['image_index']
                                            tensor = item['tensor']
                                            
                                            if worker_id not in worker_images:
                                                worker_images[worker_id] = {}
                                            worker_images[worker_id][image_index] = tensor
                                            
                                            collected_count += 1
                                        
                                        if is_last:
                                            workers_done.add(worker_id)
                                            p.update(1)  # +1 here too
                            else:
                                log(f"Master - Queue {multi_job_id} no longer exists!")
                        break
            except comfy.model_management.InterruptProcessingException:
                # Cleanup queue on interruption and re-raise to abort prompt cleanly
                async with prompt_server.distributed_jobs_lock:
                    if multi_job_id in prompt_server.distributed_pending_jobs:
                        del prompt_server.distributed_pending_jobs[multi_job_id]
                raise
            
            total_collected = sum(len(imgs) for imgs in worker_images.values())
            
            # Clean up job queue
            async with prompt_server.distributed_jobs_lock:
                if multi_job_id in prompt_server.distributed_pending_jobs:
                    del prompt_server.distributed_pending_jobs[multi_job_id]

            # Reorder images according to seed distribution pattern
            # Pattern: master img 1, master img 2, worker 1 img 1, worker 1 img 2, worker 2 img 1, worker 2 img 2, etc.
            ordered_tensors = []
            
            # Add master images first (if any)
            if not delegate_mode and images_on_cpu is not None:
                for i in range(master_batch_size):
                    ordered_tensors.append(images_on_cpu[i:i+1])
            
            # Add worker images in order
            # The worker IDs in worker_images are already strings (e.g., "1", "2")
            # Just iterate through what we actually received
            for worker_id_str in sorted(worker_images.keys()):
                # Sort by image index for each worker
                for idx in sorted(worker_images[worker_id_str].keys()):
                    ordered_tensors.append(worker_images[worker_id_str][idx])
            
            # Ensure all tensors are on CPU and properly formatted before concatenation
            cpu_tensors = []
            for t in ordered_tensors:
                if t.is_cuda:
                    t = t.cpu()
                # Ensure tensor is contiguous in memory
                t = ensure_contiguous(t)
                cpu_tensors.append(t)
            
            try:
                if cpu_tensors:
                    combined = torch.cat(cpu_tensors, dim=0)
                else:
                    # No tensors collected (likely delegate mode with no worker output)
                    combined = ensure_contiguous(images) if images is not None else images
                    if combined is None:
                        raise ValueError("No image data collected from master or workers")
                # Ensure the combined tensor is contiguous and properly formatted
                combined = ensure_contiguous(combined)
                debug_log(f"Master - Job {multi_job_id} complete. Combined {combined.shape[0]} images total (master: {master_batch_size}, workers: {combined.shape[0] - master_batch_size})")

                # Combine audio from master and workers
                combined_audio = self._combine_audio(master_audio, worker_audio, empty_audio)

                return (combined, combined_audio)
            except Exception as e:
                log(f"Master - Error combining images: {e}")
                # Return just the master images as fallback
                return (images, audio if audio is not None else empty_audio)
