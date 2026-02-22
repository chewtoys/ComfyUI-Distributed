"""
Async helper utilities for ComfyUI-Distributed.
"""
import asyncio
import threading
import uuid
import execution
import server
from typing import Optional, Any, Coroutine
from .network import get_server_loop

def run_async_in_server_loop(coro: Coroutine, timeout: Optional[float] = None) -> Any:
    """
    Run async coroutine in server's event loop and wait for result.
    
    This is useful when you need to run async code from a synchronous context
    but want to use the server's existing event loop instead of creating a new one.
    
    Args:
        coro: The coroutine to run
        timeout: Optional timeout in seconds
        
    Returns:
        The result of the coroutine
        
    Raises:
        TimeoutError: If the operation times out
        Exception: Any exception raised by the coroutine
    """
    event = threading.Event()
    result = None
    error = None
    
    async def wrapper():
        nonlocal result, error
        try:
            result = await coro
        except Exception as e:
            error = e
        finally:
            event.set()
    
    # Schedule on server's event loop
    loop = get_server_loop()
    asyncio.run_coroutine_threadsafe(wrapper(), loop)
    
    # Wait for completion
    if not event.wait(timeout):
        raise TimeoutError(f"Async operation timed out after {timeout} seconds")
    
    if error:
        raise error
    return result


prompt_server = server.PromptServer.instance


async def queue_prompt_payload(prompt_obj, workflow_meta=None, client_id=None):
    """Validate and queue a prompt via ComfyUI's prompt queue."""
    payload = {"prompt": prompt_obj}
    payload = prompt_server.trigger_on_prompt(payload)
    prompt = payload["prompt"]

    prompt_id = str(uuid.uuid4())
    valid = await execution.validate_prompt(prompt_id, prompt, None)
    if not valid[0]:
        raise RuntimeError(f"Invalid prompt: {valid[1]}")

    extra_data = {}
    if workflow_meta:
        extra_data.setdefault("extra_pnginfo", {})["workflow"] = workflow_meta
    if client_id:
        extra_data["client_id"] = client_id

    sensitive = {}
    for key in getattr(execution, "SENSITIVE_EXTRA_DATA_KEYS", []):
        if key in extra_data:
            sensitive[key] = extra_data.pop(key)

    number = getattr(prompt_server, "number", 0)
    prompt_server.number = number + 1
    prompt_queue_item = (number, prompt_id, prompt, extra_data, valid[2], sensitive)
    prompt_server.prompt_queue.put(prompt_queue_item)
    return prompt_id
