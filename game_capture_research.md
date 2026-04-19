# 游戏/应用后台画面、声音与键鼠操作捕获方案调研

本文档总结了目前在后台捕获系统画面（高帧率游戏/应用）、系统与麦克风声音，以及记录键盘和鼠标操作的主流技术方案。

## 1. 画面捕获方案 (Screen/Game Capture)

对于游戏录制而言，画面捕获的核心痛点是**高帧率 (FPS)** 和 **低延迟/低资源占用**。传统的截图方案（如 `pyautogui` 或 `Pillow`）无法满足 60FPS 以上的实时捕获需求。

### 1.1 高性能专用方案（推荐用于游戏）
*   **Windows Desktop Duplication API (DXGI) / DXcam**
    *   **简介**: 微软提供的底层 API，直接从 GPU 显存中获取画面，是目前 Windows 下性能最高的捕获方式。
    *   **Python 库**: [`DXcam`](https://github.com/ra1nty/DXcam) 是目前最流行的 Python 封装库，专为高帧率游戏录制设计（可达 240+ FPS @ 1080p）。支持 Zero-copy（零拷贝）直接输出为 NumPy 数组，非常适合结合 OpenCV 或 FFmpeg 使用。
*   **Windows Graphics Capture (WinRT)**
    *   **简介**: Windows 10/11 引入的现代捕获 API，相比 DXGI，它在捕获特定窗口（即使窗口被遮挡或在后台）时表现更好。
    *   **Python 库**: `windows-capture` (基于 Rust 编写的 Python 绑定)，性能极佳。
*   **macOS ScreenCaptureKit**
    *   **简介**: 苹果在 macOS 12.3 引入的高性能原生框架，取代了老旧的 CGWindowListCreateImage。支持极低延迟的独立窗口或全屏捕获。
    *   **调用方式**: 可以通过 PyObjC 调用原生 API，或者使用基于 AVFoundation 的 FFmpeg 命令行。

### 1.2 跨平台/通用方案
*   **OBS Studio (结合 obs-websocket)**
    *   **简介**: 工业界标准。OBS 底层已经处理好了所有平台的高性能捕获（Hook 注入游戏进程、DXGI、ScreenCaptureKit 等）。
    *   **方案**: 在后台静默运行 OBS，通过 Python 脚本使用 `obs-websocket-py` 控制录制的开始、停止，以及获取画面流。
    *   **优点**: 性能最强，兼容性最好，音视频同步完美。
*   **FFmpeg CLI**
    *   **简介**: 强大的音视频处理工具。
    *   **命令示例**:
        *   Windows: `ffmpeg -f gdigrab -framerate 60 -i desktop output.mp4` (较慢) 或使用 `ddagrab` (基于 DXGI，极快)。
        *   macOS: `ffmpeg -f avfoundation -i "<screen>:<audio>" output.mp4`。
*   **Python MSS (`python-mss`)**
    *   **简介**: 纯 Python 实现的跨平台截图库，使用 ctypes 调用系统 API。
    *   **性能**: 比 pyautogui 快得多，但在高分辨率下录制 60FPS 游戏仍有压力，适合中低帧率的屏幕记录。

---

## 2. 声音捕获方案 (Audio Capture)

捕获声音分为两部分：**麦克风输入 (Input)** 和 **系统扬声器输出 (Loopback/Output)**。

*   **SoundDevice / PyAudio**
    *   **简介**: Python 中最常用的音频处理库。`sounddevice` 基于 PortAudio，支持将音频数据直接读取为 NumPy 数组。
    *   **系统声音捕获 (Loopback)**:
        *   **Windows**: 可以使用 WASAPI 的 Loopback 模式来录制电脑发出的声音（游戏声音）。
        *   **macOS**: 原生不支持直接录制系统声音，通常需要借助虚拟声卡驱动（如 BlackHole 或 Soundflower），将系统声音路由到虚拟输入设备，然后再用 Python 读取。
*   **FFmpeg**
    *   可以直接通过 `dshow` (Windows) 或 `avfoundation` (macOS) 捕获麦克风和系统声音，并与视频流合并。

---

## 3. 键鼠操作捕获方案 (Input Logging/Hooking)

在后台静默记录玩家的键盘和鼠标操作（Keylogging & Mouse tracking）。

### 3.1 Python 主流库
*   **`pynput` (推荐)**
    *   **简介**: 跨平台库，提供监听 (Listener) 和控制键盘鼠标的功能。
    *   **原理**: 在后台运行监听线程，通过操作系统的 Hook 机制（Windows 的 `SetWindowsHookEx`，macOS 的 Quartz Event Services）捕获全局事件。
    *   **优点**: API 简单，支持后台静默记录。
*   **`keyboard` 和 `mouse`**
    *   **简介**: 提供了全局热键和底层事件 Hook 的功能。
    *   **注意**: 在 Linux 上需要 root 权限。

### 3.2 游戏环境的特殊挑战 (DirectInput / Raw Input)
*   **痛点**: 许多大型 3D 游戏（如 FPS 游戏）为了降低延迟，会绕过操作系统的标准消息队列，直接使用 **DirectInput** 或 **Raw Input** 读取硬件状态。
*   **结果**: 上述基于系统 Hook 的 Python 库（如 pynput）可能**无法在游戏全屏运行时捕获到按键**。
*   **解决方案**:
    *   如果需要捕获底层游戏输入，可能需要编写 C/C++ 驱动级代码，或者使用特定的拦截库（如 Interception API 的 Python 封装）。

---

## 4. 综合架构方案推荐

根据不同的需求，推荐以下三种组合方案：

### 方案 A：基于 OBS 的全能方案（最稳定、性能最好）
*   **架构**: OBS Studio (后台静默运行) + `obs-websocket-py` + `pynput`
*   **流程**:
    1. Python 脚本使用 `pynput` 在后台记录键鼠操作，打上时间戳。
    2. Python 脚本通过 WebSocket 发送指令给 OBS 开始录制画面和声音。
    3. 录制结束后，将键鼠操作日志 (JSON/CSV) 与 OBS 导出的高质量 MP4 视频对齐。
*   **适用场景**: 需要极高画质和帧率的 3A 游戏录制，不想自己处理音视频编码和同步的复杂逻辑。

### 方案 B：纯 Python 高性能方案（适合 AI 分析/计算机视觉）
*   **架构**: `DXcam` (画面) + `sounddevice` (声音) + `pynput` (键鼠) + `FFmpeg` (编码)
*   **流程**:
    1. 开启多进程 (Multiprocessing)。
    2. 进程 1: 使用 DXcam 抓取画面（NumPy 数组），存入 Ring Buffer。
    3. 进程 2: 使用 sounddevice 抓取音频流。
    4. 进程 3: 使用 pynput 记录键鼠。
    5. 进程 4: 从 Buffer 中取出音视频数据，通过 `subprocess` 喂给 FFmpeg 命令行进行 H.264/NVENC 硬件编码并写入文件。
*   **适用场景**: 需要在录制的同时对画面进行实时 AI 处理（如目标检测、自动高光剪辑）。

### 方案 C：轻量级跨平台脚本（适合普通应用/网页录制）
*   **架构**: `python-mss` + `PyAudio` + `OpenCV` (VideoWriter)
*   **适用场景**: 不需要 60FPS，主要用于记录办公软件、网页自动化过程，代码部署最简单，不依赖特定操作系统的底层 API。

## 5. 注意事项
1.  **权限问题**: 在 macOS 上，捕获屏幕和监听全局键盘/鼠标都需要在“系统偏好设置 -> 隐私与安全性”中授予终端或 Python 解释器**“屏幕录制”**和**“辅助功能”**权限。
2.  **音视频同步**: 自己编写多线程/多进程分别捕获音视频时，时间戳对齐 (Timestamp synchronization) 是最大的难点。如果不是为了实时处理，强烈建议直接使用 FFmpeg 或 OBS 来处理音视频合并。