# Cyberpunk 2077 Camera + Z-depth

This module records the final active Cyberpunk 2077 camera through Cyber Engine
Tweaks and captures the matching GPU depth buffer through a ReShade 6 add-on.

## Install

No Git installation is required in a packaged release. Run:

```bat
cp2077-camera\install.bat --cp2077-dir "D:\Games\Cyberpunk 2077"
```

The installer:

1. Keeps an existing compatible RED4ext/CET installation, or installs it.
2. Downloads the official ReShade full add-on setup and installs its `dxgi.dll`
   proxy for the game's D3D12 renderer.
3. Replaces the legacy `cp2077_camera_export` CET mod with `CameraFrameLogger`.
4. Installs `cp2077_depth.addon64` and `CP2077Depth.fx` under `bin\x64`.

ReShade binaries are downloaded from the official website and are not bundled
for redistribution. For a network-isolated machine, manually place the official
`ReShade_Setup_*_Addon.exe` in `cp2077-camera\vendor\ReShade\` and use
`--skip-download`.

## Record

Start the game, load into gameplay, then run the recorder normally. The recorder
publishes one shared `active_session.json`; the CET camera logger and ReShade
depth add-on both start and stop with that session.

A completed session contains:

| Path | Contents |
|---|---|
| `camera.jsonl` | Per-video-frame camera intrinsics and extrinsics |
| `depth.jsonl` | Video-frame to depth-file alignment |
| `depth/depth_*.npy` | `H x W`, little-endian `float32` camera Z-depth in metres |
| `meta.json` | Schema, alignment statistics, axes and depth calibration |

`camera.jsonl` schema `cp2077_camera_v3` includes:

- `intrinsic`: `fx`, `fy`, `cx`, `cy`, image width and height.
- `world_to_camera`: row-major 4x4 OpenCV extrinsic for column vectors.
- `camera_to_world`: inverse row-major 4x4 extrinsic.
- `rotation_world_to_camera` and `translation_world_to_camera`.
- `world_to_pixel`: row-major 3x4 matrix `K [R | t]`.
- `camera_position_world`, active camera axes and vertical/horizontal FOV.

The camera convention is OpenCV: `+X` right, `+Y` down, `+Z` forward. Therefore
each value in a depth NPY is strictly `Zc` in:

```text
X_camera = R_world_to_camera X_world + t_world_to_camera
camera Z-depth = X_camera[2]
```

It is not camera-to-point Euclidean distance. Pixel back-projection is:

```text
Xc = (u - cx) / fx * Zc
Yc = (v - cy) / fy * Zc
Zc = depth[v, u]
```

Cyberpunk 2077 device depth is converted with an empirical calibration based on
the public `jasonbunk/reshade_cv` Cyberpunk curve. The exact constants and source
are embedded in every session's `meta.json` and raw depth header.

## Performance

Depth is copied through a four-slot asynchronous GPU readback ring and written by
a bounded worker queue. Even so, uncompressed float depth is large: about 8 MB per
1080p sample or 33 MB per 4K sample. At 5 fps this is roughly 40 MB/s or 165 MB/s.

## Uninstall

```bat
cp2077-camera\uninstall.bat --cp2077-dir "D:\Games\Cyberpunk 2077"
```

This removes only `CameraFrameLogger`, `cp2077_depth.addon64` and
`CP2077Depth.fx`. It leaves shared RED4ext, CET and ReShade runtimes installed.
