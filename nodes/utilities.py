import torch
import json

from ..utils.logging import debug_log, log


def _divide_and_pad(items: list, n_workers: int) -> list[list]:
    """Split items across n_workers buckets, padding each to length 10 with the last item."""
    if not items:
        return [[] for _ in range(max(1, n_workers))]

    buckets = [[] for _ in range(max(1, n_workers))]
    for i, item in enumerate(items):
        buckets[i % len(buckets)].append(item)

    for bucket in buckets:
        while len(bucket) < 10:
            bucket.append(bucket[-1] if bucket else items[-1])

    return buckets


class DistributedSeed:
    """
    Distributes seed values across multiple GPUs.
    On master: passes through the original seed.
    On workers: adds offset based on worker ID.
    """
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "seed": ("INT", {
                    "default": 1125899906842, 
                    "min": 0,
                    "max": 1125899906842624,
                    "forceInput": False  # Widget by default, can be converted to input
                }),
            },
            "hidden": {
                "is_worker": ("BOOLEAN", {"default": False}),
                "worker_id": ("STRING", {"default": ""}),
            },
        }
    
    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("seed",)
    FUNCTION = "distribute"
    CATEGORY = "utils"
    
    def distribute(self, seed, is_worker=False, worker_id=""):
        if not is_worker:
            # Master node: pass through original values
            debug_log(f"Distributor - Master: seed={seed}")
            return (seed,)
        else:
            # Worker node: apply offset based on worker index
            # Find worker index from enabled_worker_ids
            try:
                # Worker IDs are passed as "worker_0", "worker_1", etc.
                if worker_id.startswith("worker_"):
                    worker_index = int(worker_id.split("_")[1])
                else:
                    # Fallback: try to parse as direct index
                    worker_index = int(worker_id)
                
                offset = worker_index + 1
                new_seed = seed + offset
                debug_log(f"Distributor - Worker {worker_index}: seed={seed} → {new_seed}")
                return (new_seed,)
            except (ValueError, IndexError) as e:
                debug_log(f"Distributor - Error parsing worker_id '{worker_id}': {e}")
                # Fallback: return original seed
                return (seed,)

# Define ByPassTypeTuple for flexible return types
class AnyType(str):
    def __ne__(self, __value: object) -> bool:
        return False

any_type = AnyType("*")

class DistributedModelName:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"default": ""}),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = (any_type,)
    RETURN_NAMES = ("output",)
    FUNCTION = "log_input"
    OUTPUT_NODE = True
    CATEGORY = "utils"

    def _stringify(self, value):
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        try:
            return json.dumps(value, indent=4)
        except Exception:
            return str(value)

    def _update_workflow(self, extra_pnginfo, unique_id, values):
        if not extra_pnginfo:
            return
        info = extra_pnginfo[0] if isinstance(extra_pnginfo, list) else extra_pnginfo
        if not isinstance(info, dict) or "workflow" not in info:
            return
        node_id = None
        if isinstance(unique_id, list) and unique_id:
            node_id = str(unique_id[0])
        elif unique_id is not None:
            node_id = str(unique_id)
        if not node_id:
            return
        workflow = info["workflow"]
        node = next((x for x in workflow["nodes"] if str(x.get("id")) == node_id), None)
        if node:
            node["widgets_values"] = [values]

    def log_input(self, text, unique_id=None, extra_pnginfo=None):
        values = []
        if isinstance(text, list):
            for val in text:
                values.append(self._stringify(val))
        else:
            values.append(self._stringify(text))

        # Keep widget display in workflow metadata if available.
        self._update_workflow(extra_pnginfo, unique_id, values)

        if isinstance(values, list) and len(values) == 1:
            return {"ui": {"text": values}, "result": (values[0],)}
        return {"ui": {"text": values}, "result": (values,)}

class ByPassTypeTuple(tuple):
    def __getitem__(self, index):
        if index > 0:
            index = 0
        item = super().__getitem__(index)
        if isinstance(item, str):
            return any_type
        return item

class ImageBatchDivider:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "images": ("IMAGE",),
                "divide_by": ("INT", {
                    "default": 2, 
                    "min": 1, 
                    "max": 10, 
                    "step": 1,
                    "display": "number",
                    "tooltip": "Number of parts to divide the batch into"
                }),
            }
        }
    
    RETURN_TYPES = ByPassTypeTuple(("IMAGE", ))  # Flexible for variable outputs
    RETURN_NAMES = ByPassTypeTuple(tuple([f"batch_{i+1}" for i in range(10)]))
    FUNCTION = "divide_batch"
    OUTPUT_NODE = True
    CATEGORY = "image"
    
    def divide_batch(self, images, divide_by):
        import torch

        total_splits = max(1, min(int(divide_by), 10))
        total_frames = images.shape[0]

        if total_frames > 0:
            frame_indices = list(range(total_frames))
            buckets = _divide_and_pad(frame_indices, total_splits)
            outputs = [images[bucket] for bucket in buckets]
            empty_tensor = torch.zeros(
                (1, images.shape[1], images.shape[2], images.shape[3]),
                dtype=images.dtype,
                device=images.device,
            )
        else:
            outputs = []
            empty_tensor = torch.zeros((1, 512, 512, 3), dtype=torch.float32)

        while len(outputs) < 10:
            outputs.append(empty_tensor)

        return tuple(outputs[:10])


class AudioBatchDivider:
    """Divides an audio waveform into multiple parts along the time/samples dimension."""

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "audio": ("AUDIO",),
                "divide_by": ("INT", {
                    "default": 2,
                    "min": 1,
                    "max": 10,
                    "step": 1,
                    "display": "number",
                    "tooltip": "Number of parts to divide the audio into"
                }),
            }
        }

    RETURN_TYPES = ByPassTypeTuple(("AUDIO",))  # Flexible for variable outputs
    RETURN_NAMES = ByPassTypeTuple(tuple([f"audio_{i+1}" for i in range(10)]))
    FUNCTION = "divide_audio"
    OUTPUT_NODE = True
    CATEGORY = "audio"

    def divide_audio(self, audio, divide_by):
        import torch

        waveform = audio.get("waveform")
        sample_rate = audio.get("sample_rate", 44100)

        if waveform is None or waveform.numel() == 0:
            # Return empty audio for all outputs
            empty_audio = {"waveform": torch.zeros(1, 2, 1), "sample_rate": sample_rate}
            return tuple([empty_audio] * 10)

        total_splits = max(1, min(int(divide_by), 10))

        # Waveform shape: [batch, channels, samples]
        total_samples = waveform.shape[-1]
        sample_indices = list(range(total_samples))
        buckets = _divide_and_pad(sample_indices, total_splits)

        outputs = []
        for bucket in buckets:
            outputs.append({
                "waveform": waveform[..., bucket],
                "sample_rate": sample_rate
            })

        # Pad with empty audio up to max (10) to match RETURN_TYPES length
        empty_audio = {
            "waveform": torch.zeros(waveform.shape[0], waveform.shape[1], 1,
                                    dtype=waveform.dtype, device=waveform.device),
            "sample_rate": sample_rate
        }

        while len(outputs) < 10:
            outputs.append(empty_audio)

        return tuple(outputs)


class DistributedEmptyImage:
    """Produces an empty IMAGE batch used when the master delegates all work."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "height": ("INT", {"default": 64, "min": 1, "max": 4096, "step": 1}),
                "width": ("INT", {"default": 64, "min": 1, "max": 4096, "step": 1}),
                "channels": ("INT", {"default": 3, "min": 1, "max": 4, "step": 1}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "create"
    CATEGORY = "image"

    def create(self, height, width, channels):
        import torch

        shape = (0, height, width, channels)
        tensor = torch.zeros(shape, dtype=torch.float32)
        return (tensor,)
