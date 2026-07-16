# Cyberpunk 2077 Camera

通过 **Cyber Engine Tweaks (CET)** 采集赛博朋克 2077 的相机外参（`camera_to_world`）与内参（`intrinsic`、`world_to_clip`、FOV、视口）。

运行项目根目录 **`install.bat`** 时会自动尝试：

1. 发现本机赛博朋克 2077 安装目录（Steam manifest / 常见路径 / `CP2077_DIR`）
2. 安装 **RED4ext** + **Cyber Engine Tweaks**（若尚未安装）
3. 部署 **CameraFrameLogger**，并在 `.cp2077_camera/install.json` 登记 CET 模组目录

完成后直接 **`run.bat`** 录制即可；停止后 session 内会有 `camera.jsonl`。

CET 将文件访问限制在模组目录内，因此录制器会把控制文件写到
`CameraFrameLogger/active_session.json`，模组在同目录生成临时 raw JSONL；停止录制后，
录制器读取并对齐该文件，再输出到 session 的 `camera.jsonl`。

## 手动安装 / 重装

```bat
cp2077-camera\install.bat
cp2077-camera\install.bat --cp2077-dir "D:\SteamLibrary\steamapps\common\Cyberpunk 2077"
```

若 `install.bat` 未找到游戏，可设置环境变量后重跑：

```bat
set CP2077_DIR=D:\SteamLibrary\steamapps\common\Cyberpunk 2077
install.bat
```

## 离线环境

将以下文件放入 vendor 目录后，安装器使用 `--skip-download` 不再联网：

- `cp2077-camera\vendor\RED4ext\red4ext-*.zip`
- `cp2077-camera\vendor\CET\cet_*.zip`

在线打包机可预缓存：

```bat
python scripts\install_cp2077_camera.py --prefetch-deps
```

## 卸载

```bat
cp2077-camera\uninstall.bat
```

## 录制流程

1. 启动赛博朋克 2077（确保 CET 已加载模组，可在 CET 控制台看到 `[CameraFrameLogger] Loaded`）
2. 运行 `run.bat` 开始录制
3. 停止录制后，session 目录会生成对齐后的 `camera.jsonl`

## 输出字段（`cp2077_camera_v2`）

每帧样本包含：

| 字段 | 说明 |
|------|------|
| `camera_to_world` | 4×4 行主序外参矩阵（row-vector，单位：米） |
| `intrinsic` | 针孔内参 `{fx, fy, cx, cy, width, height}` |
| `world_to_clip` | 由 FOV + 视口推导的透视投影矩阵 |
| `fov_horizontal_deg` / `fov_vertical_deg` | 水平/垂直 FOV（度） |
| `fov_axis` | `horizontal`（步行）或 `vertical`（载具） |
| `viewport_px` | 游戏窗口分辨率 |
| `near_plane` / `far_plane` | 近/远裁剪面（默认 0.05 / 10000） |
| `camera_mode` | `fpp` / `tpp` / `vehicle` / `player` |

坐标系约定（见 raw header `geometry`）：

- 世界轴：X 右、Y 上、Z 前
- 相机轴：X 前（视线）、Y 右、Z 上

## 限制

- 步行时 FOV 取自图形设置（水平 FOV）；载具第一人称为垂直 FOV。
- 相机位置取自玩家 `GetWorldPosition` / `GetWorldTransform`，与真实眼球位置可能有少量偏差。
- 菜单/过场中没有玩家实体时会跳过采样。
- 首次启动游戏时 CET 可能要求绑定 Overlay 热键，绑定一次即可。

## 禁用同步

录制时若不想发布 CP2077 相机信号：

```bat
run.bat --no-cp2077-camera
```
