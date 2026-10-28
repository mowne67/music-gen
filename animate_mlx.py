"""Batched Apple GPU effects, with the same timing and geometry as animate.py."""
import numpy as np
import mlx.core as mx

from animate import FPS, LOOP, W, H, light_sweeps, rain_layer


# One GPU thread renders one pixel, combining effects without intermediate images.
_RENDER = mx.fast.metal_kernel(
    name="pixel_art_effects",
    input_names=["base", "far", "near", "mask", "flicker", "sweeps", "shifts", "flies", "noise"],
    output_names=["out"],
    source=r'''
        uint index = thread_position_in_grid.x;
        if (index >= flicker_shape[0] * HEIGHT * WIDTH) return;
        uint frame = index / (HEIGHT * WIDTH);
        uint pixel = index % (HEIGHT * WIDTH);
        uint y = pixel / WIDTH, x = pixel % WIDTH;
        float3 image(base[pixel * 3], base[pixel * 3 + 1], base[pixel * 3 + 2]);
        if (FLAGS & 1) {
            float lum = dot(image, float3(0.299f, 0.587f, 0.114f));
            float glow = clamp((lum - 0.55f) / 0.3f, 0.0f, 1.0f);
            image *= 1.0f + glow * flicker[frame];
        }
        if (FLAGS & 2) {
            for (uint i = 0; i < 5; ++i) {
                uint offset = (frame * 5 + i) * 3;
                float distance = (float(x) - sweeps[offset]) / sweeps[offset + 2];
                image += sweeps[offset + 1] * exp(-distance * distance) * float3(1.0f, 0.8f, 0.5f);
            }
        }
        if (FLAGS & 4) {
            uint fy = (y + HEIGHT - shifts[frame * 2]) % HEIGHT;
            uint ny = (y + HEIGHT - shifts[frame * 2 + 1]) % HEIGHT;
            float alpha = max(far[fy * WIDTH + x], near[ny * WIDTH + x]) * mask[pixel];
            image = image * (1.0f - alpha) + float3(0.8f, 0.85f, 0.95f) * alpha;
        }
        if (FLAGS & 8) {
            for (uint i = 0; i < 14; ++i) {
                uint offset = (frame * 14 + i) * 3;
                if (x == uint(flies[offset]) && y == uint(flies[offset + 1])) {
                    float brightness = flies[offset + 2];
                    image = image * (1.0f - brightness) + float3(1.0f, 0.95f, 0.6f) * brightness;
                }
            }
        }
        if (FLAGS & 16) image += noise[index] * 0.012f;
        image = clamp(image, 0.0f, 1.0f) * 255.0f;
        out[index * 3] = uchar(image.x);
        out[index * 3 + 1] = uchar(image.y);
        out[index * 3 + 2] = uchar(image.z);
    ''',
)


def frames(base, effects, seconds=LOOP, seed=7, start_frame=0, frame_count=None, batch_size=16):
    """Render bounded batches; explicit per-frame noise keys preserve subset/wrap replay."""
    n = round(FPS * seconds)
    if n <= 0 or batch_size <= 0:
        raise ValueError("Loop duration and batch size must be positive")
    if base.shape != (H, W, 3):
        raise ValueError(f"Expected a {W}x{H} RGB base image")
    rng = np.random.default_rng(seed)
    far, near = rain_layer(700, 0.3, rng), rain_layer(250, 0.5, rng)
    mask = np.zeros((H, W), np.float32)
    x0, y0, x1, y1 = effects.get("rain") or (0, 0, 1, 1)
    mask[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)] = 1
    orbits = rng.uniform([0, H * 0.3, 4, 0], [W, H, 14, 2 * np.pi], (14, 4))
    events = light_sweeps(seconds, seed)
    flags = sum(bit for name, bit in [("flicker", 1), ("sweep", 2), ("rain", 4),
                                      ("fireflies", 8), ("grain", 16)] if name in effects)
    count = n if frame_count is None else frame_count
    with mx.stream(mx.gpu):
        static = [mx.array(a, dtype=mx.float32) for a in (base, far, near, mask)]
        mx.eval(*static)
        for first in range(start_frame, start_frame + count, batch_size):
            indices = np.arange(first, min(first + batch_size, start_frame + count)) % n
            size = len(indices)
            t = indices / n
            flicker = (0.025 * np.sin(2 * np.pi * round(seconds / 5) * t)
                       + 0.012 * np.sin(2 * np.pi * round(seconds / 1.7) * t + 0.4))
            sweeps = np.zeros((size, 5, 3), np.float32)
            for i, event in enumerate(events):
                elapsed = (indices - event["start_frame"]) % n
                u = elapsed / event["duration_frames"]
                sweeps[:, i, 0] = -100 + (W + 200) * u
                sweeps[:, i, 1] = np.where(elapsed < event["duration_frames"],
                                         event["brightness"] * np.sin(np.pi * u) ** 2, 0)
                sweeps[:, i, 2] = event["width"]
            shifts = np.stack([
                np.rint(H * (max(1, round(seconds / 2)) * t + 0.2 * np.sin(2 * np.pi * t))),
                np.rint(H * (max(1, round(seconds * 0.75)) * t + 0.15 * np.sin(4 * np.pi * t))),
            ], axis=1).astype(np.int32) % H
            cx, cy, radius, ph = orbits.T
            x = (cx + radius * np.sin(2 * np.pi * t[:, None] + ph)).astype(np.int32) % W
            y = np.clip(cy + radius * np.cos(4 * np.pi * t[:, None] + ph), 0, H - 1).astype(np.int32)
            brightness = 0.5 + 0.5 * np.sin(2 * np.pi * t[:, None] + ph)
            flies = np.stack([x, y, brightness], axis=-1).astype(np.float32)
            if "grain" in effects:
                # Each frame gets its own key, independent of batch boundaries.
                keys = mx.stack([mx.random.key((seed + 1000 + int(f)) % 2**32) for f in indices])
                noise = mx.vmap(lambda key: mx.random.normal((H, W), key=key))(keys)
            else:
                noise = mx.zeros((1,), dtype=mx.float32)
            dynamic = [mx.array(a, dtype=dtype) for a, dtype in [
                (flicker, mx.float32), (sweeps, mx.float32), (shifts, mx.int32), (flies, mx.float32),
            ]]
            batch = _RENDER(inputs=[*static, *dynamic, noise],
                            template=[("WIDTH", W), ("HEIGHT", H), ("FLAGS", flags)],
                            grid=(size * H * W, 1, 1), threadgroup=(256, 1, 1),
                            output_shapes=[(size, H, W, 3)], output_dtypes=[mx.uint8])[0]
            mx.eval(batch)
            # FFmpeg's stdin expects host bytes; transfer once per batch, not per effect.
            yield from np.asarray(batch)
