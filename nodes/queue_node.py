import asyncio
import json

import aiohttp
import server

from ..utils.config import load_config
from ..utils.logging import debug_log, log
from ..utils.async_helpers import run_async_in_server_loop
from ..utils.network import get_client_session, build_worker_url


class DistributedQueueNode:
    """Routes entire workflows to the least-busy worker for job scheduling/load balancing."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "client_id": "CLIENT_ID",
                "skip_dispatch": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("worker_id", "prompt_id")
    FUNCTION = "queue"
    OUTPUT_NODE = True
    CATEGORY = "utils"

    def queue(self, prompt=None, extra_pnginfo=None, client_id=None, skip_dispatch=False):
        if skip_dispatch:
            debug_log("DistributedQueue: skip_dispatch enabled; not dispatching.")
            return ("", "")
        if not prompt:
            log("DistributedQueue: No prompt supplied; skipping dispatch.")
            return ("", "")

        return run_async_in_server_loop(
            self._queue_on_worker(prompt, extra_pnginfo, client_id)
        )

    async def _queue_on_worker(self, prompt_obj, workflow_meta, client_id):
        config = load_config()
        enabled_workers = [
            worker for worker in config.get("workers", [])
            if worker.get("enabled", False)
        ]

        if not enabled_workers:
            log("DistributedQueue: No enabled workers found.")
            return ("", "")

        statuses = await self._fetch_worker_statuses(enabled_workers)
        if not statuses:
            log("DistributedQueue: No reachable workers found.")
            return ("", "")

        selected = self._select_worker(statuses)
        worker = selected["worker"]
        queue_remaining = selected["queue_remaining"]

        prompt_copy = json.loads(json.dumps(prompt_obj))
        self._mark_skip_dispatch(prompt_copy)

        payload = {"prompt": prompt_copy}
        extra_data = {}
        if workflow_meta:
            extra_data.setdefault("extra_pnginfo", {})["workflow"] = workflow_meta
        if client_id:
            extra_data["client_id"] = client_id
        if extra_data:
            payload["extra_data"] = extra_data

        url = build_worker_url(worker, "/prompt")
        session = await get_client_session()
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            resp.raise_for_status()
            response_payload = await resp.json()

        prompt_id = str(response_payload.get("prompt_id", ""))
        worker_id = str(worker.get("id", ""))
        log(
            f"DistributedQueue: queued prompt {prompt_id} on worker {worker_id} (queue_remaining={queue_remaining})."
        )
        return (worker_id, prompt_id)

    async def _fetch_worker_statuses(self, workers):
        session = await get_client_session()
        statuses = []
        for worker in workers:
            url = build_worker_url(worker, "/prompt")
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    queue_remaining = int(data.get("exec_info", {}).get("queue_remaining", 0))
                    statuses.append({
                        "worker": worker,
                        "queue_remaining": queue_remaining,
                    })
            except Exception as e:
                debug_log(f"DistributedQueue: Worker status check failed ({url}): {e}")

        return statuses

    def _select_worker(self, statuses):
        idle_workers = [status for status in statuses if status["queue_remaining"] == 0]
        if idle_workers:
            return self._select_round_robin(idle_workers)
        return min(statuses, key=lambda status: status["queue_remaining"])

    def _select_round_robin(self, statuses):
        prompt_server = server.PromptServer.instance
        if not hasattr(prompt_server, "distributed_queue_rr_index"):
            prompt_server.distributed_queue_rr_index = 0

        index = prompt_server.distributed_queue_rr_index % len(statuses)
        prompt_server.distributed_queue_rr_index += 1
        return statuses[index]

    def _mark_skip_dispatch(self, prompt_obj):
        for node in prompt_obj.values():
            if isinstance(node, dict) and node.get("class_type") == "DistributedQueue":
                inputs = node.setdefault("inputs", {})
                inputs["skip_dispatch"] = True
