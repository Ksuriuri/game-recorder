# CP2077 depth add-on

`cp2077_depth.addon64` is a ReShade 6.7.3 API 18 add-on for Cyberpunk 2077
(Direct3D 12 through the `dxgi.dll` proxy).
While `CameraFrameLogger/active_session.json` says that the recorder is active,
it asynchronously reads the `R32_FLOAT` target produced by `CP2077Depth.fx` and
writes one little-endian `float32` NumPy array per depth sample.

Each output value is camera Z-depth (`Zc`) in metres in the OpenCV camera frame
(`+X` right, `+Y` down, `+Z` forward). It is not Euclidean ray distance.

The device-depth calibration constants are based on the public Cyberpunk 2077
curve in `jasonbunk/reshade_cv`. The add-on validates the sampled device depth,
applies the calibrated mapping, and records the constants and provenance in
`depth_raw_cp2077.jsonl`.

Build with Visual Studio 2019 or newer:

```bat
build.bat
```

The binary is written to `cp2077-camera\dist\cp2077_depth.addon64`.
