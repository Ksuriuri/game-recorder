# Game Recorder

游戏数据采集工具 —— 同步录制游戏画面、系统音频和键鼠操作。

## 功能

- **视频捕获**：基于 DXGI Desktop Duplication API（DXcam），零拷贝直读 GPU 显存，对游戏帧率无影响
- **无边框游戏区域自动捕获**：默认 `--capture-mode auto`，按下热键开始录制时会优先捕获当前前台的大客户区窗口（适合 Borderless Windowed），识别不到合适窗口时自动回退整屏
- **音频捕获**：默认走 **Python `soundcard` 包的 WASAPI Loopback**（抓当前 Windows 默认播放设备的混音），通过本机 TCP 把 PCM 喂给 FFmpeg，与视频在同一 FFmpeg 进程内 mux 实现天然同步。**零配置、不依赖 Stereo Mix、不需要装虚拟声卡、不需要管理员权限**，是网吧 / GTA 之类 shared-mode 游戏的标准录音通路。如果当前 FFmpeg 构建恰好带 `wasapi` indev（罕见），优先用单进程 WASAPI；都不行再回退到 DirectShow（Stereo Mix / VB-CABLE 等）
- **键鼠捕获**：键盘和鼠标优先走 Win32 Raw Input，避免低级鼠标钩子影响游戏视角；Raw Input 不可用时键盘降级为 `GetAsyncKeyState` 轮询
- **硬件编码**：自动检测 NVIDIA NVENC，使用 GPU 专用编码单元，不占用 CUDA 核心；无 NVENC 时回退到 `libx264 ultrafast`，默认限制 2 个 x264 线程，避免网吧机器上抢占游戏 CPU
- **统一时钟**：所有数据流共享 `perf_counter_ns` 高精度 T0 基准，同步误差 < 1 帧（33ms）
- **可选分段保存**：通过 `--segment-minutes N` 每隔 N 分钟落盘一对 `mp4 + jsonl` 文件（默认关闭，推荐保持关闭以获得无缝音视频；启用时段间会有约几百毫秒的空隙）
- **录制状态悬浮窗**：屏幕右上角显示当前段已录制时长与 **累计有效视频时长**（全库汇总，每次录制结束后刷新）；默认后台启动、无黑色终端，通过悬浮窗 **退出** 正常结束程序
- **自动停止录制**：录制过程中仅允许 **WASD** 移动人物；**10 秒未按 WASD** 或 **按下其他键盘按键**（鼠标移动/点击不算）会自动结束当前段并弹出居中提示，**连按两次大写键（Caps Lock）** 重新开始录制后提示关闭

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
3. 下载 FFmpeg（[BtbN gpl 构建](https://github.com/BtbN/FFmpeg-Builds)，约 140 MB，含 NVENC、libx264、DirectShow 等编码器/复用器）
4. 创建 `.venv` 并 `uv pip install -e .`（`soundcard` 这个 Python 包就是默认音频通路的关键，pyproject.toml 里已经声明）
5. 生成启动脚本 `run.bat`（默认无窗口后台运行；`--console` 显示终端；`--list-audio-devices` / `--no-overlay` 自动带控制台）

> **关于音频**：BtbN / 上游 win64 静态构建几乎都**没有编译 `wasapi` indev**，所以本工具的默认音频通路其实是 Python 端的 `soundcard` WASAPI loopback（抓默认扬声器混音 → s16le → 本机 TCP → FFmpeg）。这条路径不依赖 FFmpeg 构建带不带 wasapi、也不依赖 Stereo Mix 是否启用，是网吧场景能开箱跑通的关键。如果你换的 FFmpeg 构建恰好带 `wasapi` indev，会自动优先用单进程的 wasapi（一点 CPU 优化），但不是必须。

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

### 方式三：离线便携包（网吧 / 无网环境）

如果目标机器**没有网络**（网吧、内网机、不能科学上网的环境），按下面两步：

#### 在有网的开发机上构建

```bat
scripts\build_offline_bundle.bat
```

脚本会：

1. 跑一遍 `install.bat`（联网下载 uv / 托管 Python 3.11 / FFmpeg / 所有依赖 wheel 进 uv 缓存并建好 `.venv`）
2. `uv pip freeze` 锁定解析后的精确版本（含 numpy / opencv-python-headless / dxcam / soundcard / cffi / pycparser…）
3. `uv pip download` 把这些 wheel 全量保存到 `wheels\`
4. 删掉 `.venv\`（venv 的 `pyvenv.cfg` 写死了绝对路径，搬到别的机器就崩，所以不进包；目标机器会从 `wheels\` 几秒内重建）
5. `Compress-Archive` 打成 `game-recorder-portable-YYYYMMDD.zip`（约 400-450 MB，取决于托管 Python / FFmpeg / wheel 缓存版本）

#### 离线包内容

`game-recorder-portable-YYYYMMDD.zip` 解压后是一个自包含目录（无 `.venv\`，目标机器首次跑 `install.bat` 会从 `wheels\` 重建）：

```
game-recorder/
├── install.bat                 # 在目标机上离线重建 .venv（自动检测 wheels/ 切到 OFFLINE 模式）
├── run.bat / run-console.bat   # 一键启动（install 也会从 scripts\ 同步一份）
├── 录制操作手册.md             # 网吧/采集同学用的简版说明
├── pyproject.toml              # 让 uv pip install -e . 知道项目本体
├── README.md
├── src/                        # 项目源码（editable 安装的目标）
├── scripts/                    # build_offline_bundle.bat 等
├── ffmpeg/bin/ffmpeg.exe       # ~140 MB，BtbN gpl 构建
├── wheels/                     # ~55 MB，所有 runtime 依赖的 wheel
│   ├── numpy-*.whl
│   ├── opencv_python_headless-*.whl
│   ├── dxcam-*.whl
│   ├── SoundCard-*.whl
│   └── cffi-*.whl, pycparser-*.whl …
└── .tools/
    ├── uv/uv.exe               # ~15 MB，独立 uv
    ├── python/                 # ~30 MB，托管的 cpython 3.11
    └── uv-cache/               # uv 解析缓存（双保险，--find-links 不命中时兜底）
```

#### 在网吧 PC 上部署

1. U 盘把 zip 拷过去
2. 解压到 **`D:\game-recorder\`**（**别放 C 盘**——网吧的还原系统重启就把 C 盘清回原状）
3. 双击 `install.bat`：横幅出现 `Mode : OFFLINE (restoring from local wheels/)` 即说明检测到离线包；脚本会用 `--offline --no-index --find-links wheels\` 重建 `.venv`，全程不碰网络，约 10 秒
4. 双击 `run.bat` → 右上角悬浮窗出现 → **连按两次 Caps Lock（大写键）** 开始/停止录制 → 结束时点悬浮窗 **退出**

整个 zip 自包含，不写注册表，不写 `%LOCALAPPDATA%` / `%APPDATA%`，卸载就是删目录。

> **更新源码后想重打包**：直接再跑一次 `scripts\build_offline_bundle.bat`，它会清掉旧的 `wheels\` 重新下，时间戳后缀也会更新到当天。

#### 录不到声音时的最小排错流程

按顺序检查，**90% 的网吧场景在第 1 步就能定位**：

1. **看启动日志**：`run.bat` 默认不显示终端，请用 `run.bat --console` 启动；若出现下面两条，音频通路就没问题，问题在别处（比如游戏静音）：
   ```
   Audio: Python WASAPI loopback via soundcard (default speaker → TCP 127.0.0.1:xxxxx → FFmpeg).
   Python loopback streaming to FFmpeg (s16le 48000 Hz x2).
   ```
2. **跑 `run.bat --list-audio-devices`** 看这一行：
   ```
   Python soundcard loopback (default speaker): yes
   ```
   - `yes` 但实际录到静音 → 通常是 Windows "声音 → 输出" 的默认设备选错了（比如默认到了关掉的 HDMI 显示器），改一下默认输出设备
   - `no` → soundcard 包没装好，或者驱动异常；`run.bat --console -v` 看调试日志里的 `soundcard loopback not available: ...`
3. **看 `meta.json` 的 `audio_source` 字段**判断实际走了哪条路径：
   - `"soundcard:default"` → Python loopback（最常见、最可靠）
   - `"wasapi:default"` → 走了 FFmpeg 原生 wasapi（极少数构建）
   - `"dshow:Stereo Mix (...)"` → 降级到 DirectShow，可用但不推荐
   - `"dshow:VoiceMeeter Output ..."` → 这条**通常是静音**，需要改 Windows 默认输出去 VoiceMeeter，或者换通路
   - `null` → 静音录制；上面三条全失败时的兜底

## 使用

### Windows 一键安装后

```bat
:: 默认：后台无终端，右上角悬浮窗，连按两次大写键切换录制，点「退出」结束
run.bat

:: 显示黑色终端（看启动日志、Ctrl+C 退出、配合 -v 调试）
run.bat --console

:: 立即开始录制（无需热键）
run.bat --no-hotkey

:: 自定义参数
run.bat --fps 30 --quality 23 --output ./data --mouse-hz 30 --segment-minutes 5

:: 调整空闲自动停止阈值（秒）；0 = 关闭“长时间未移动”检测（非 WASD 按键检测仍生效）
run.bat --idle-timeout 15
run.bat --idle-timeout 0

::: GTA5 / 网吧机器卡顿时，降低采集与软件编码压力
run.bat --fps 20 --quality 28 --x264-threads 1

::: 强制录当前前台窗口客户区；或用 screen 强制整屏
run.bat --capture-mode foreground
run.bat --capture-mode screen

:: 调试模式（需带 --console 才能在终端看到日志）
run.bat --console -v

:: 列出音频设备（自动打开控制台）
run.bat --list-audio-devices
```

#### `run.bat` 启动方式

| 方式 | 命令 | 说明 |
|------|------|------|
| 默认（推荐） | `run.bat` | `pythonw` 后台运行，无黑色终端；用悬浮窗 **退出** 结束 |
| 控制台 | `run.bat --console` … | 显示终端，可用 **Ctrl+C** 退出；`--console` 后的参数原样传给 `game-recorder` |
| 自动控制台 | `run.bat --list-audio-devices` | 需要打印设备列表 |
| 自动控制台 | `run.bat --no-overlay` | 无悬浮窗时没有「退出」入口，保留终端供 **Ctrl+C** 退出 |

### 手动安装后

```bash
# 启动后连按两次 Caps Lock（大写键）开始/停止录制
game-recorder

# 立即开始录制（无需热键）
game-recorder --no-hotkey

# 自定义参数
game-recorder --fps 30 --quality 23 --output ./data --mouse-hz 30 --segment-minutes 5

# 调整空闲自动停止阈值（秒）；0 = 关闭“长时间未移动”检测
game-recorder --idle-timeout 15
game-recorder --idle-timeout 0

# GTA5 / 网吧机器卡顿时，降低采集与软件编码压力
game-recorder --fps 20 --quality 28 --x264-threads 1

# 强制录当前前台窗口客户区；或用 screen 强制整屏
game-recorder --capture-mode foreground
game-recorder --capture-mode screen

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
| `--mouse-hz` | 30 | 鼠标移动采样率（Hz） |
| `--x264-threads` | 2 | 无 NVENC 时 `libx264` 软件编码可用的 CPU 线程数；游戏卡顿时可设为 `1` |
| `--segment-minutes` | 0 | 每隔多少分钟自动切分一段 mp4 + jsonl，`0`（默认）表示关闭分段、整次录制写入单文件 |
| `--capture-mode` | `auto` | `auto`：自动捕获前台的大客户区窗口，否则整屏；`foreground`：尽量强制前台客户区；`screen`：整屏 |
| `--idle-timeout` | 10 | 未按 WASD 移动人物超过 N 秒则自动停止录制；`0` 关闭此项（按下非 WASD 键仍会停止） |
| `--no-hotkey` | - | 跳过热键，立即开始录制 |
| `--no-overlay` | - | 关闭游戏内录制状态悬浮窗 |
| `-v` | - | 输出调试日志 |

### 快捷键

| 按键 | 功能 |
|------|------|
| 连按两次 Caps Lock（大写键） | 开始/停止录制 |
| Ctrl+C | 停止录制并退出程序（`--console` 模式） |
| 悬浮窗「退出」 | 停止录制并退出程序（默认后台模式） |

**连按两次 Caps Lock（大写键）** 通过全局按键轮询检测；全屏游戏下一般仍可用。手动停止/重新开始录制时按下的 **大写键不会** 触发“非 WASD 按键”自动停止。

### 自动停止录制

为采集“纯人物移动”片段，录制期间有两条自动停止规则（均会保存当前段并弹出居中提示）：

| 触发条件 | 提示文案（摘要） |
|----------|------------------|
| **10 秒**内未按 **WASD**（按住 WASD 也算有操作） | 由于长时间未移动人物角色，本次录制已自动结束 |
| 按下 **非 WASD** 的键盘按键（**鼠标移动、点击、滚轮不算**） | 检测到按下了非人物移动的按键，本次录制已自动结束 |

提示框会一直保留，直到再次 **连按两次 Caps Lock（大写键）** 开始新一段录制后自动关闭。可用 `--idle-timeout N` 调整空闲阈值，或 `--idle-timeout 0` 仅关闭“长时间未移动”检测；**非 WASD 按键检测无法单独关闭**。

### 录制状态悬浮窗

默认在屏幕右上角显示一个小悬浮窗（状态区鼠标穿透，右上角 **退出** 可正常结束程序）。在 Windows 10 2004 及以上，悬浮窗与自动停止提示会通过系统 API 标记为「不参与屏幕捕获」：你仍能在显示器上看到，但 DXGI 录屏（本程序使用的 DXcam）一般不会把它录进 `mp4`。

| 状态 | 显示内容 |
|------|----------|
| 未录制 | `未开始录制`、开始录制提示、**累计有效视频时长** |
| 录制中 | `正在录制`、当前段 **已录制** 时长（每 0.5 秒刷新）、停止提示、**累计有效视频时长**（冻结为上次结束时的值，不随本段增长） |

**累计有效视频时长** 的含义与更新规则：

- **统计范围**：`--output` 目录（默认 `recordings/`）下，所有已保存 session 里各段 `mp4` 的有效时长之和。
- **不算入**：时长不足被丢弃的 session、`*_inputs.mp4` 等衍生文件；**不读取 mp4**（用 `meta.json` 里 `segments[].frame_count ÷ fps` 汇总，与画面长度一致）。
- **“有效”**：因 **空闲自动停止**（长时间未按 WASD）结束时，会从末段视频裁掉末尾约 `--idle-timeout` 秒对应的帧（`meta.json` 的 `idle_tail_trim_frames`）；若尚未裁剪，则按 `duration_s − idle_timeout_s` 计入累计。手动停止或其它自动停止原因按实际视频时长计入。
- **何时刷新**：仅在**每次录制成功落盘并写入 `library.json` 之后**更新（`session.stop()` 完成之后）；录制过程中不读盘、不叠加本段秒数。
- **索引文件**：`recordings/library.json` 由程序维护；首次启动或文件缺失时，后台扫描所有 `session_*/meta.json` 重建一次。

自动停止时会在屏幕**居中偏上**弹出红色醒目提示（见上一节），并周期性置顶，尽量显示在游戏窗口之上。

`run.bat` 默认用无窗口方式在后台启动（不弹出黑色终端）。需要看启动日志或调试时用 `run.bat --console`；`run.bat --list-audio-devices` 会自动带控制台输出。

注意：独占全屏游戏可能不允许普通桌面窗口盖在游戏上方；建议把游戏显示模式改成“窗口模式 / 无边框窗口”。不需要悬浮窗时可以用 `run.bat --no-overlay`（会自动保留控制台，以便 Ctrl+C 退出；自动停止时改为在控制台打印相同提示）。

## 多机 / 网吧部署注意事项

针对"把整个目录拷到任意 Windows 机器（如网吧）就能直接录"的场景：

- **不要装在系统盘**。`install.bat` 会检测当前盘符，若在 `C:\` 会要求二次确认。网吧普遍装有"还原系统 / 影子系统"，重启后 C 盘会被清回原状，含本工具和所有录制文件。建议放在 `D:\game-recorder\` 之类。
- **后台常驻**：`run.bat` 默认不弹终端，适合网吧双击即用；排错或确认音频时用 `run.bat --console` 看启动日志。结束程序请点悬浮窗 **退出**，不要到任务管理器里强杀（可能丢未落盘的段）。
- **音频零配置（重要）**：默认链路是 **Python `soundcard` 包打开默认扬声器的 WASAPI loopback**，把 s16le PCM 通过本机 TCP 喂给 FFmpeg 一起 mux 进 mp4。无需启用 Stereo Mix、无需装 VB-CABLE/VoiceMeeter、不需要管理员权限、对 FFmpeg 构建零要求。用 `run.bat --console` 启动后，日志里看到 `Audio: Python WASAPI loopback via soundcard ...` 和 `Python loopback streaming to FFmpeg ...` 两条就说明声音通路 OK；最终 `meta.json` 的 `audio_source` 是 `"soundcard:default"`。
- **想确认这台机器能不能录到声**：到目录下跑 `run.bat --list-audio-devices`，关注两行：
  - `Python soundcard loopback (default speaker): yes`  → 能用，到此为止，无需任何额外配置
  - `Python soundcard loopback (default speaker): no`   → 极少数情况（驱动问题 / 默认设备配置异常），再考虑 enable Stereo Mix 或 `--audio-device`
- **录制前别动音频设备**：录制开始时把"默认播放设备"快照下来，录制中如果**插拔耳机 / 切换输出设备**导致 Windows 切换默认设备，本次录制会继续录原设备（很可能从这一刻起变静音）。需要换设备的话，请先 **连按两次大写键** 停止再切。
- **GTA 等使用 shared-mode 音频的游戏可直接录**。极少数**强制独占模式**的应用会让 WASAPI loopback 拿到静音；本工具自动降级到 DirectShow，再不行就静音录制（`meta.json` 的 `audio_source` 会是 `null`，便于事后过滤）。
- **NVENC 跨机泛化**：网吧 GPU 五花八门，不一定是 N 卡。代码里已经做了 NVENC 运行时探测：编译启用但驱动不给开 → 自动落到 `libx264 ultrafast`，并默认限制 `--x264-threads 2`。如果 GTA5 等游戏仍然卡，优先用 `run.bat --fps 20 --quality 28 --x264-threads 1`。
- **切屏 / 全屏切换**：DXGI 在 Alt+Tab 或游戏切全屏时可能短暂报告不同分辨率。录制器会把临时尺寸缩放回本次 session 的初始尺寸，避免视频花屏或被切成多段；真正 0 帧的启动空段会自动清理。

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
  library.json                          # 全库累计有效视频时长索引（悬浮窗读取）
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
  "audio_source": "soundcard:default",
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
  ],
  "auto_stop_reason": null,
  "idle_timeout_s": 10.0,
  "idle_tail_trim_frames": 0
}
```

- `auto_stop_reason`：`null` 为手动停止；`"idle"` / `"forbidden_key"` 表示自动停止原因（影响累计有效时长的计算方式，见上文悬浮窗说明）。
- `idle_tail_trim_frames`：空闲自动停止后从末段裁掉的帧数；`> 0` 时累计时长直接按裁剪后的 `segments` 帧数计算。

### library.json

位于输出目录根部的轻量索引，供悬浮窗快速读取 **累计有效视频时长**，无需扫描全部 `meta.json` 或解析 mp4：

```json
{
  "sessions": {
    "session_20260411_143022": {
      "duration_s": 1394.3,
      "video_count": 3
    }
  }
}
```

每次 session 成功保存后更新对应条目；删除某个 `session_*` 目录后若累计不准，可删除 `library.json`，下次启动会自动重建。

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
  ├─ overlay.py  右上角状态悬浮窗 + 自动停止居中提示（鼠标穿透 + 「退出」）
  └─ session.py  Session 生命周期，统一 T0 时钟
       ├─ capture/screen.py      DXcam 帧捕获循环
       ├─ capture/input_hook.py        Win32 Raw Input 键鼠捕获
       ├─ encoder/ffmpeg_pipe.py       FFmpeg 子进程（rawvideo pipe + 音频选路）
       ├─ encoder/python_loopback.py   默认音频通路：soundcard 抓默认扬声器 → s16le → 本机 TCP → FFmpeg
       └─ storage/
            ├─ action_writer.py        JSONL 缓冲写入
            ├─ session_writer.py       meta.json 序列化
            ├─ library_index.py        library.json 累计有效视频时长索引
            └─ idle_trim.py            空闲自动停止末段裁剪
```

## 性能开销（1080p@30fps）

| 组件 | CPU | GPU | 磁盘 |
|------|-----|-----|------|
| DXcam 帧捕获 | ~3% 单核 | ~0% | - |
| FFmpeg NVENC 编码 | ~2% 单核 | 编码单元（不影响游戏） | 8-12 MB/s |
| 音频（soundcard WASAPI loopback） | ~0.3% 单核 | - | - |
| Raw Input 输入捕获 | ~0.1% | - | < 0.1 MB/s |
| 状态悬浮窗 | 可忽略 | - | 录制结束后读一次 `library.json` |
| FFmpeg libx264 fallback | 受 `--x264-threads` 限制，默认最多 2 线程 | - | 取决于 `--quality` |
| **合计** | **~5%** | **~0%** | **~10 MB/s** |
