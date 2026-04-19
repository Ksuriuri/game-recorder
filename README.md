# Game Recorder

游戏世界模型训练数据采集工具 —— 同步录制游戏画面、系统音频和键鼠操作。

## 功能

- **视频捕获**：基于 DXGI Desktop Duplication API（DXcam），零拷贝直读 GPU 显存，对游戏帧率无影响
- **音频捕获**：通过 FFmpeg 捕获 WASAPI Loopback 系统声音，与视频在同一进程中 mux，天然同步
- **键鼠捕获**：Win32 低级钩子（`WH_KEYBOARD_LL` / `WH_MOUSE_LL`），事件按视频帧索引对齐
- **硬件编码**：自动检测 NVIDIA NVENC，使用 GPU 专用编码单元，不占用 CUDA 核心；无 NVENC 时回退到 libx264 ultrafast
- **统一时钟**：所有数据流共享 `perf_counter_ns` 高精度 T0 基准，同步误差 < 1 帧（33ms）
- **可选分段保存**：通过 `--segment-minutes N` 每隔 N 分钟落盘一对 `mp4 + jsonl` 文件（默认关闭，推荐保持关闭以获得无缝音视频；启用时段间会有约几百毫秒的空隙）

## 系统要求

- Windows 10/11
- Python 3.10+
- FFmpeg（需在 PATH 中或放入项目 `ffmpeg/` 目录）
- NVIDIA GPU（可选，用于 NVENC 硬件编码）

## 安装

### 方式一：一键安装（Windows，推荐）

双击项目根目录的 `install.bat`，脚本会自动完成：

1. 下载独立版 `uv`
2. 通过 uv 安装托管的 Python 3.11
3. 下载 FFmpeg essentials build
4. 创建 `.venv` 并 `uv pip install -e .`
5. 生成启动脚本 `run.bat`

**所有文件全部落在项目目录下，不占用系统盘**：

```
game-recorder/
├── .venv/                 # Python 虚拟环境
├── ffmpeg/bin/ffmpeg.exe  # 本地 FFmpeg
└── .tools/
    ├── uv/uv.exe
    ├── python/            # uv 管理的 Python（UV_PYTHON_INSTALL_DIR）
    └── uv-cache/          # wheel 缓存（UV_CACHE_DIR）
```

卸载时直接删除整个项目目录即可，注册表 / `%LOCALAPPDATA%` / `%APPDATA%` 无任何残留。

### 方式二：手动安装

```bash
uv pip install -e .
```

需自行准备 FFmpeg 并放入 PATH 或项目 `ffmpeg/` 目录。

## 使用

### Windows 一键安装后

```bat
:: 启动后按 Ctrl+F9 开始/停止录制
run.bat

:: 立即开始录制（无需热键）
run.bat --no-hotkey

:: 自定义参数
run.bat --fps 60 --quality 18 --output ./data --mouse-hz 500 --segment-minutes 5

:: 调试模式
run.bat -v
```

### 手动安装后

```bash
# 启动后按 Ctrl+F9 开始/停止录制
game-recorder

# 立即开始录制（无需热键）
game-recorder --no-hotkey

# 自定义参数
game-recorder --fps 60 --quality 18 --output ./data --mouse-hz 500 --segment-minutes 5

# 调试模式
game-recorder -v
```

### 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--fps` | 30 | 目标捕获帧率 |
| `--output` | `./recordings` | 输出目录 |
| `--quality` | 23 | 视频质量（CQ 值，越低质量越高，文件越大） |
| `--audio-device` | 自动检测 | DirectShow 音频设备名 |
| `--mouse-hz` | 200 | 鼠标移动采样率（Hz） |
| `--segment-minutes` | 0 | 每隔多少分钟自动切分一段 mp4 + jsonl，`0`（默认）表示关闭分段、整次录制写入单文件 |
| `--no-hotkey` | - | 跳过热键，立即开始录制 |
| `-v` | - | 输出调试日志 |

### 快捷键

| 按键 | 功能 |
|------|------|
| Ctrl+F9 | 开始/停止录制 |
| Ctrl+C | 停止录制并退出程序 |

## 输出格式

每次录制生成一个 session 目录，里面包含一对或多对 `mp4 + jsonl` 文件，外加一份 `meta.json`。文件命名统一遵循：

```
{session_timestamp}_{start_frame}_{end_frame}.{mp4|jsonl}
```

- `session_timestamp` 是**整次 session 开始录制的时间戳**（所有段共用，便于按 session 分组）
- `start_frame` / `end_frame` 是**全局帧索引**（不在每段内部重置），`end_frame` 为开区间（不包含）
- 末段的 `end_frame` 等于用户停止时实际录到的总帧数，因此长度可能小于一个完整分段

### 默认（`--segment-minutes 0`，单文件）

```
recordings/
  session_20260411_143022/
    20260411_143022_0_42130.mp4
    20260411_143022_0_42130.jsonl
    meta.json
```

### 启用分段（如 `--segment-minutes 10`）

```
recordings/
  session_20260411_143022/
    20260411_143022_0_18000.mp4          # 第 1 段（帧 0 至 18000，不含 18000）
    20260411_143022_0_18000.jsonl
    20260411_143022_18000_36000.mp4      # 第 2 段，start_frame 衔接上一段的 end_frame
    20260411_143022_18000_36000.jsonl
    20260411_143022_36000_42130.mp4      # 末段（实际录到的帧数）
    20260411_143022_36000_42130.jsonl
    meta.json
```

> **注意**：分段切换时会有约几百毫秒的视频/音频空隙（FFmpeg 进程关停 + 重启 + WASAPI 设备重新打开），这段时间的帧索引照常前进但不会写入任何文件。如果对连续性敏感，请保持默认 `--segment-minutes 0`。

### *.jsonl

**按视频帧聚合**：每行一个 JSON 对象，对应一帧；`frame` 是**全局帧索引**（与文件名里的范围保持一致，`start_frame` 起的相对帧 = `frame - start_frame`），`events` 是该帧时间窗口内 (`[frame/fps, (frame+1)/fps)`) 捕获到的所有键鼠事件，按发生先后排序。**没有事件的帧不会出现在文件中**。

帧索引计算：`frame = int((t_event_ns - t0_ns) * fps / 1e9)`

```jsonl
{"frame":0,"events":[{"type":"key","action":"down","vk":87,"key":"W"}]}
{"frame":1,"events":[{"type":"mouse","action":"move","x":960,"y":540}]}
{"frame":3,"events":[{"type":"mouse","action":"left_down","x":800,"y":400},{"type":"mouse","action":"left_up","x":800,"y":400}]}
{"frame":4,"events":[{"type":"mouse","action":"scroll","x":960,"y":540,"scroll_delta":120}]}
{"frame":7,"events":[{"type":"key","action":"up","vk":87,"key":"W"}]}
```

### meta.json

```json
{
  "session_id": "session_20260411_143022",
  "session_timestamp": "20260411_143022",
  "start_epoch_ms": 1744364222000,
  "duration_s": 1404.3,
  "fps": 30,
  "resolution": [1920, 1080],
  "encoder": "h264_nvenc",
  "foreground_window": "Game Title",
  "total_frames": 42130,
  "total_input_events": 95812,
  "segment_seconds": 600,
  "segments": [
    {
      "index": 0, "start_frame": 0, "end_frame": 18000,
      "frame_count": 18000, "event_count": 41203,
      "video": "20260411_143022_0_18000.mp4",
      "actions": "20260411_143022_0_18000.jsonl"
    },
    {
      "index": 1, "start_frame": 18000, "end_frame": 36000,
      "frame_count": 18000, "event_count": 39872,
      "video": "20260411_143022_18000_36000.mp4",
      "actions": "20260411_143022_18000_36000.jsonl"
    },
    {
      "index": 2, "start_frame": 36000, "end_frame": 42130,
      "frame_count": 6130, "event_count": 14737,
      "video": "20260411_143022_36000_42130.mp4",
      "actions": "20260411_143022_36000_42130.jsonl"
    }
  ]
}
```

## 训练数据读取

由于事件按全局帧索引聚合、且 jsonl 与 mp4 同名同段，读取时只需按段配对加载即可。下面的 helper 顺序遍历整个 session 的所有段：

```python
import cv2
import json
from pathlib import Path


def iter_session(session_dir: str):
    meta = json.loads(Path(session_dir, "meta.json").read_text())
    for seg in meta["segments"]:
        video = cv2.VideoCapture(str(Path(session_dir, seg["video"])))
        with open(Path(session_dir, seg["actions"])) as f:
            actions_by_frame = {
                rec["frame"]: rec["events"]
                for rec in (json.loads(line) for line in f)
            }

        frame_idx = seg["start_frame"]  # global frame index
        while True:
            ret, frame = video.read()
            if not ret:
                break
            yield frame_idx, frame, actions_by_frame.get(frame_idx, [])
            frame_idx += 1
        video.release()
```

## 架构

```
main.py          CLI 入口，热键监听
  └─ session.py  Session 生命周期，统一 T0 时钟
       ├─ capture/screen.py      DXcam 帧捕获循环
       ├─ capture/input_hook.py  Win32 低级键鼠钩子
       ├─ encoder/ffmpeg_pipe.py FFmpeg 子进程（rawvideo pipe + WASAPI 音频）
       └─ storage/
            ├─ action_writer.py  JSONL 缓冲写入
            └─ session_writer.py 元数据序列化
```

## 性能开销（1080p@30fps）

| 组件 | CPU | GPU | 磁盘 |
|------|-----|-----|------|
| DXcam 帧捕获 | ~3% 单核 | ~0% | - |
| FFmpeg NVENC 编码 | ~2% 单核 | 编码单元（不影响游戏） | 8-12 MB/s |
| WASAPI 音频 | ~0.1% | - | - |
| Win32 输入钩子 | ~0.1% | - | < 0.1 MB/s |
| **合计** | **~5%** | **~0%** | **~10 MB/s** |
