# Game Recorder

游戏世界模型训练数据采集工具 —— 同步录制游戏画面、系统音频和键鼠操作。

## 功能

- **视频捕获**：基于 DXGI Desktop Duplication API（DXcam），零拷贝直读 GPU 显存，对游戏帧率无影响
- **音频捕获**：通过 FFmpeg 捕获 WASAPI Loopback 系统声音，与视频在同一进程中 mux，天然同步
- **键鼠捕获**：Win32 低级钩子（`WH_KEYBOARD_LL` / `WH_MOUSE_LL`），亚毫秒级时间戳精度
- **硬件编码**：自动检测 NVIDIA NVENC，使用 GPU 专用编码单元，不占用 CUDA 核心；无 NVENC 时回退到 libx264 ultrafast
- **统一时钟**：所有数据流共享 `perf_counter_ns` 高精度 T0 基准，同步误差 < 1 帧（33ms）

## 系统要求

- Windows 10/11
- Python 3.10+
- FFmpeg（需在 PATH 中或放入项目 `ffmpeg/` 目录）
- NVIDIA GPU（可选，用于 NVENC 硬件编码）

## 安装

```bash
uv pip install -e .
```

## 使用

```bash
# 启动后按 Ctrl+F9 开始/停止录制
game-recorder

# 立即开始录制（无需热键）
game-recorder --no-hotkey

# 自定义参数
game-recorder --fps 60 --quality 18 --output ./data --mouse-hz 500

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
| `--no-hotkey` | - | 跳过热键，立即开始录制 |
| `-v` | - | 输出调试日志 |

### 快捷键

| 按键 | 功能 |
|------|------|
| Ctrl+F9 | 开始/停止录制 |
| Ctrl+C | 停止录制并退出程序 |

## 输出格式

每次录制生成一个 session 目录：

```
recordings/
  session_20260411_143022/
    video.mp4          # H.264 + AAC 视频（含系统音频）
    actions.jsonl      # 键鼠事件流（JSONL 格式）
    meta.json          # 录制元数据
```

### actions.jsonl

每行一个 JSON 对象，`t` 字段为毫秒时间戳（与视频第 0 帧对齐）：

```jsonl
{"t":0.00,"type":"key","action":"down","vk":87,"key":"W"}
{"t":16.67,"type":"mouse","action":"move","x":960,"y":540}
{"t":33.34,"type":"mouse","action":"left_down","x":800,"y":400}
{"t":100.50,"type":"key","action":"up","vk":87,"key":"W"}
{"t":150.00,"type":"mouse","action":"scroll","x":960,"y":540,"scroll_delta":120}
```

帧对应关系：`frame_index = int(t_ms * fps / 1000)`

### meta.json

```json
{
  "session_id": "session_20260411_143022",
  "start_epoch_ms": 1744364222000,
  "duration_s": 3600.5,
  "fps": 30,
  "resolution": [1920, 1080],
  "encoder": "h264_nvenc",
  "foreground_window": "Game Title",
  "total_frames": 108015,
  "total_input_events": 245830
}
```

## 训练数据读取

```python
import cv2
import json


def iter_session(session_dir: str, fps: int = 30):
    video = cv2.VideoCapture(f"{session_dir}/video.mp4")
    actions = [json.loads(line) for line in open(f"{session_dir}/actions.jsonl")]
    action_idx = 0

    while True:
        ret, frame = video.read()
        if not ret:
            break
        frame_t_ms = video.get(cv2.CAP_PROP_POS_MSEC)

        frame_actions = []
        while action_idx < len(actions) and actions[action_idx]["t"] <= frame_t_ms:
            frame_actions.append(actions[action_idx])
            action_idx += 1

        yield frame, frame_actions  # (H, W, 3) ndarray, List[dict]

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
