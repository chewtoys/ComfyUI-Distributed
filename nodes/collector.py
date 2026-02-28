import torch
import io
import json
import asyncio
import time
import base64

import aiohttp
import server as _server
import comfy.model_management
from comfy.utils import ProgressBar

from ..utils.logging import debug_log, log
from ..utils.config import get_worker_timeout_seconds, load_config, is_master_delegate_only
from ..utils.constants import HEARTBEAT_INTERVAL
from ..utils.image import tensor_to_pil, pil_to_tensor, ensure_contiguous
from ..utils.network import build_worker_url, get_client_session, probe_worker
from ..utils.audio_payload import encode_audio_payload
from ..utils.async_helpers import run_async_in_server_loop

prompt_server = _server.PromptServer.instance


class DistributedCollectorNode:
    EMPTY_AUDIO = {"waveform": torch.zeros(1, 2, 1), "sample_rate": 44100}

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
                debug_log("Collector: pass-through mode enabled, returning images unchanged")
            return (images, audio if audio is not None else empty_audio)

        # Use async helper to run in server loop
        result = run_async_in_server_loop(
            self.execute(images, audio, multi_job_id, is_worker, master_url, enabled_worker_ids, worker_batch_size, worker_id, delegate_only)
        )
        return result

    async def send_batch_to_master(self, image_batch, audio, multi_job_id, master_url, worker_id):
        """Send image batch to master via canonical JSON envelopes."""
        batch_size = image_batch.shape[0]
        if batch_size == 0:
            return

        encoded_audio = encode_audio_payload(audio)

        session = await get_client_session()
        url = f"{master_url}/distributed/job_complete"
        for batch_idx in range(batch_size):
            img = tensor_to_pil(image_batch[batch_idx:batch_idx+1], 0)
            byte_io = io.BytesIO()
            img.save(byte_io, format='PNG', compress_level=0)
            encoded_image = base64.b64encode(byte_io.getvalue()).decode('utf-8')
            payload = {
                "job_id": str(multi_job_id),
                "worker_id": str(worker_id),
                "batch_idx": int(batch_idx),
                "image": f"data:image/png;base64,{encoded_image}",
                "is_last": bool(batch_idx == batch_size - 1),
            }
            if payload["is_last"] and encoded_audio is not None:
                payload["audio"] = encoded_audio

            try:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as response:
                    response.raise_for_status()
            except Exception as e:
                log(f"Worker - Failed to send canonical image envelope to master: {e}")
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
            log(f"[Distributed] Master - Audio combination failed, returning silence: {e}")
            return empty_audio

    def _store_worker_result(self, worker_images: dict, item: dict) -> int:
        """Store one canonical queue item in worker_images in-place.

        Canonical format:
        - item has 'worker_id', 'image_index', and 'tensor'
        Returns 1 when stored, otherwise 0.
        """
        worker_id = item['worker_id']
        tensor = item.get('tensor')
        image_index = item.get('image_index')
        if tensor is None or image_index is None:
            return 0

        worker_images.setdefault(worker_id, {})
        worker_images[worker_id][image_index] = tensor
        return 1

    def _reorder_and_combine_tensors(
        self,
        worker_images: dict,
        master_batch_size: int,
        images_on_cpu,
        delegate_mode: bool,
        fallback_images,
    ) -> torch.Tensor:
        """Assemble final tensor: master images first, then workers sorted by ID and index."""
        ordered_tensors = []
        if not delegate_mode and images_on_cpu is not None:
            for i in range(master_batch_size):
                ordered_tensors.append(images_on_cpu[i:i+1])
        for worker_id_str in sorted(worker_images.keys()):
            for idx in sorted(worker_images[worker_id_str].keys()):
                ordered_tensors.append(worker_images[worker_id_str][idx])

        cpu_tensors = []
        for t in ordered_tensors:
            if t.is_cuda:
                t = t.cpu()
            t = ensure_contiguous(t)
            cpu_tensors.append(t)

        if cpu_tensors:
            return ensure_contiguous(torch.cat(cpu_tensors, dim=0))
        elif fallback_images is not None:
            return ensure_contiguous(fallback_images)
        else:
            raise ValueError("No image data collected from master or workers")

    async def execute(self, images, audio, multi_job_id="", is_worker=False, master_url="", enabled_worker_ids="[]", worker_batch_size=1, worker_id="", delegate_only=False):
        if is_worker:
            # Worker mode: send images and audio to master in a single batch
            debug_log(f"Worker - Job {multi_job_id} complete. Sending {images.shape[0]} image(s) to master")
            await self.send_batch_to_master(images, audio, multi_job_id, master_url, worker_id)
            return (images, audio if audio is not None else self.EMPTY_AUDIO)
        else:
            delegate_mode = delegate_only or is_master_delegate_only()
            # Master mode: collect images and audio from workers
            enabled_workers = json.loads(enabled_worker_ids)
            num_workers = len(enabled_workers)
            if num_workers == 0:
                return (images, audio if audio is not None else self.EMPTY_AUDIO)

            # Create the queue before any expensive local work to avoid job_complete race.
            async with prompt_server.distributed_jobs_lock:
                if multi_job_id not in prompt_server.distributed_pending_jobs:
                    prompt_server.distributed_pending_jobs[multi_job_id] = asyncio.Queue()
                    debug_log(f"Master - Initialized queue early for job {multi_job_id}")
                else:
                    existing_size = prompt_server.distributed_pending_jobs[multi_job_id].qsize()
                    debug_log(f"Master - Using existing queue for job {multi_job_id} (current size: {existing_size})")

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
            
            # Collect images until all workers report they're done
            collected_count = 0
            workers_done = set()
            
            # Use unified worker timeout from config/UI with simple sliced waits
            base_timeout = float(get_worker_timeout_seconds())
            slice_timeout = min(max(0.1, HEARTBEAT_INTERVAL / 20.0), base_timeout)
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
                        count = self._store_worker_result(worker_images, result)
                        collected_count += count
                        debug_log(
                            f"Master - Got canonical result from worker {worker_id}, "
                            f"image {result.get('image_index', 0)}, is_last={is_last}"
                        )

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
                        elapsed = time.time() - last_activity
                        for missing_worker_id in sorted(missing_workers):
                            log(
                                "Master - Heartbeat timeout: "
                                f"worker={missing_worker_id}, elapsed={elapsed:.1f}s"
                            )
                        log(
                            f"Master - Heartbeat timeout. Still waiting for workers: {list(missing_workers)} "
                            f"(elapsed={elapsed:.1f}s)"
                        )

                        # Probe missing workers' /prompt endpoints to check if they are actively processing
                        any_busy = False
                        try:
                            cfg = load_config()
                            cfg_workers = cfg.get('workers', [])
                            for wid in list(missing_workers):
                                wrec = next((w for w in cfg_workers if str(w.get('id')) == str(wid)), None)
                                if not wrec:
                                    debug_log(f"Collector probe: worker {wid} not found in config")
                                    continue
                                worker_url = build_worker_url(wrec)
                                try:
                                    payload = await probe_worker(worker_url, timeout=2.0)
                                    queue_remaining = None
                                    if payload is not None:
                                        queue_remaining = int(payload.get('exec_info', {}).get('queue_remaining', 0))
                                    debug_log(
                                        "Collector probe: worker "
                                        f"{wid} online={payload is not None} queue_remaining={queue_remaining}"
                                    )
                                    if payload is not None and queue_remaining and queue_remaining > 0:
                                        any_busy = True
                                        log(
                                            f"Master - Probe grace: worker {wid} appears busy "
                                            f"(queue_remaining={queue_remaining}). Continuing to wait."
                                        )
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

                                        collected_count += self._store_worker_result(worker_images, item)
                                        
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

            try:
                combined = self._reorder_and_combine_tensors(
                    worker_images, master_batch_size, images_on_cpu, delegate_mode, images
                )
                debug_log(f"Master - Job {multi_job_id} complete. Combined {combined.shape[0]} images total "
                          f"(master: {master_batch_size}, workers: {combined.shape[0] - master_batch_size})")

                # Combine audio from master and workers
                combined_audio = self._combine_audio(master_audio, worker_audio, self.EMPTY_AUDIO)

                return (combined, combined_audio)
            except Exception as e:
                log(f"Master - Error combining images: {e}")
                # Return just the master images as fallback
                return (images, audio if audio is not None else self.EMPTY_AUDIO)
