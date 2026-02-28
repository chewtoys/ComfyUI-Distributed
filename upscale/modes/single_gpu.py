import math, torch
from PIL import Image
from ...utils.logging import debug_log, log
from ...utils.image import tensor_to_pil, pil_to_tensor


class SingleGpuModeMixin:
    def process_single_gpu(self, upscaled_image, model, positive, negative, vae,
                          seed, steps, cfg, sampler_name, scheduler, denoise,
                          tile_width, tile_height, padding, mask_blur, force_uniform_tiles, tiled_decode):
        """Process all tiles on a single GPU (no distribution), batching per tile like USDU."""
        # Round tile dimensions
        tile_width = self.round_to_multiple(tile_width)
        tile_height = self.round_to_multiple(tile_height)

        # Get image dimensions and batch size
        batch_size, height, width, _ = upscaled_image.shape

        # Calculate all tiles
        all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)

        rows = math.ceil(height / tile_height)
        cols = math.ceil(width / tile_width)
        log(
            f"USDU Dist: Single GPU | Canvas {width}x{height} | Tile {tile_width}x{tile_height} | Grid {rows}x{cols} ({len(all_tiles)} tiles/image) | Batch {batch_size}"
        )

        # Prepare result images list
        result_images = []
        for b in range(batch_size):
            image_pil = tensor_to_pil(upscaled_image[b:b+1], 0).convert('RGB')
            result_images.append(image_pil.copy())

        # Precompute tile masks once
        tile_masks = []
        for tx, ty in all_tiles:
            tile_masks.append(self.create_tile_mask(width, height, tx, ty, tile_width, tile_height, mask_blur))

        # Process tiles batched across images
        for tile_idx, (tx, ty) in enumerate(all_tiles):
            # Progressive state parity: extract each tile from the current updated image batch.
            source_batch = torch.cat([pil_to_tensor(img) for img in result_images], dim=0)
            if upscaled_image.is_cuda:
                source_batch = source_batch.cuda()

            # Extract batched tile
            tile_batch, x1, y1, ew, eh = self.extract_batch_tile_with_padding(
                source_batch, tx, ty, tile_width, tile_height, padding, force_uniform_tiles
            )

            # Process batch
            region = (x1, y1, x1 + ew, y1 + eh)
            processed_batch = self.process_tiles_batch(tile_batch, model, positive, negative, vae,
                                                       seed, steps, cfg, sampler_name, scheduler, denoise,
                                                       tiled_decode, region, (width, height))

            # Blend results back into each image using cached mask
            tile_mask = tile_masks[tile_idx]
            for b in range(batch_size):
                tile_pil = tensor_to_pil(processed_batch, b)
                # Resize back to extracted size
                if tile_pil.size != (ew, eh):
                    tile_pil = tile_pil.resize((ew, eh), Image.LANCZOS)
                result_images[b] = self.blend_tile(result_images[b], tile_pil, x1, y1, (ew, eh), tile_mask, padding)

        # Convert back to tensor
        result_tensors = [pil_to_tensor(img) for img in result_images]
        result_tensor = torch.cat(result_tensors, dim=0)
        if upscaled_image.is_cuda:
            result_tensor = result_tensor.cuda()

        return (result_tensor,)
