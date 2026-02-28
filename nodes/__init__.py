from .utilities import (
    DistributedSeed,
    DistributedModelName,
    ImageBatchDivider,
    AudioBatchDivider,
    DistributedEmptyImage,
    AnyType,
    ByPassTypeTuple,
    any_type,
)
from .collector import DistributedCollectorNode

NODE_CLASS_MAPPINGS = {
    "DistributedCollector": DistributedCollectorNode,
    "DistributedSeed": DistributedSeed,
    "DistributedModelName": DistributedModelName,
    "ImageBatchDivider": ImageBatchDivider,
    "AudioBatchDivider": AudioBatchDivider,
    "DistributedEmptyImage": DistributedEmptyImage,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DistributedCollector": "Distributed Collector",
    "DistributedSeed": "Distributed Seed",
    "DistributedModelName": "Distributed Model Name",
    "ImageBatchDivider": "Image Batch Divider",
    "AudioBatchDivider": "Audio Batch Divider",
    "DistributedEmptyImage": "Distributed Empty Image",
}
