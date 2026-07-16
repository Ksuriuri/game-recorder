# Wukong Camera

《黑神话：悟空》的 UE4SS 相机采集 payload。目录结构保持为安装目标相对路径：

- `payload/dwmapi.dll`
- `payload/ue4ss/UE4SS.dll`
- `payload/ue4ss/UE4SS-settings.ini`
- `payload/ue4ss/VTableLayout.ini`
- `payload/ue4ss/Mods/mods.txt`
- `payload/ue4ss/Mods/CameraFrameLogger/Scripts/main.lua`

## 安装与卸载

必须先完全退出游戏。推荐从项目目录双击：

```bat
wukong-camera\install.bat
wukong-camera\uninstall.bat
```

安装器接受游戏根目录或 `b1\Binaries\Win64`：

```bat
wukong-camera\install.bat --wukong-dir "D:\SteamLibrary\steamapps\common\BlackMythWukong"
wukong-camera\uninstall.bat --wukong-dir "D:\SteamLibrary\steamapps\common\BlackMythWukong"
```

也可以设置 `WUKONG_DIR`。无人值守安装可传 `--no-prompt`；自动发现不到游戏时
返回 exit code `3`（跳过），安装成功返回 `0`，失败返回 `1`。参考游戏 exe
应为 `728458376` 字节；版本不匹配时，交互安装必须明确输入大写 `YES`，
无人值守则必须显式传入 `--force-version`。

安装器会从 Steam registry、常见 Steam 目录、各盘符的 `Steam` /
`SteamLibrary` 以及 `libraryfolders.vdf` 自动发现游戏。写入受保护的游戏目录时
会请求 UAC，并等待提权后的安装进程结束后透传退出码。安装只复制
`payload-manifest.json` 管理的静态文件，不会启动游戏。

## 安全与恢复

安装前会校验 payload 每个文件的字节数和 SHA256，安装后再校验一次。首次安装时，
如果 Win64 已有 `dwmapi.dll` 或 `ue4ss`，会完整备份到游戏根目录下的
`.game_recorder_wukong_camera`。升级始终保留这份首次安装前备份。
若 Git 在 Windows checkout 时只把受管文本的 LF 转成 CRLF，安装器会先规范化换行；
只有规范化后字节数和 SHA256 与 manifest 完全一致才继续，其他内容变化仍会拒绝。
已有 `Mods/mods.txt` 会保留其他模组条目，只更新 `CameraFrameLogger : 1`，不会整表覆盖。
旧版 UE4SS 的 `xinput1_3.dll` 加载器与本包的 `dwmapi.dll` 不能共存；安装器会将其
一并事务备份并暂时移除，卸载时原样恢复。

每次安装/升级都会先快照当前 `dwmapi.dll`、完整 `ue4ss`、安装状态和同步控制文件；
复制、配置或校验失败时自动回滚，不保留半安装状态。卸载器必须读到有效 state
才会删除本安装管理的 `dwmapi.dll` 和 `ue4ss`，随后恢复首次安装前的完整备份。
它会校验 Win64、备份边界和安装后文件所有权，不会根据猜测删除游戏目录外的路径。
如果安装后新增/修改了其他 UE4SS 文件，卸载会拒绝执行并保留现场，避免不可逆删除；
请先自行备份或还原这些改动后再卸载。若安装进程被强制结束或机器断电，下次安装/
卸载会优先校验持久事务快照的完整文件清单、字节数和 SHA256，再恢复到变更前状态；
未完成或校验失败的快照绝不会覆盖当前游戏文件。
升级同样会校验上次安装管理的 DLL/配置；除可安全合并的 `mods.txt` 外，检测到用户
修改就拒绝覆盖。

## 会话控制

安装器会在游戏的 `b1/Binaries/Win64/ue4ss/Mods/CameraFrameLogger/config.lua`
动态生成以下配置（该文件不属于静态 payload，也不进入 manifest）：

```lua
return { control_file = "D:/game-recorder-parent/.wukong_camera/active_session.json" }
```

控制文件位于 recordings 目录的父目录下；安装器会以原子替换方式初始化为 `idle`。
路径统一使用 `/`，支持中文和空格。录制器开始/停止 session 时也会原子更新这个文件。

插件在 idle 状态下只以 100 ms 周期读取 control JSON。进入 `recording` 后，它会覆盖写
`session_dir/raw_file`（默认 `camera_raw_wukong.jsonl`），输出
`wukong_camera_v2` JSONL；录制结束后 game-recorder 会将其同步整理为 session 内的
`camera.jsonl`。

每条 sample 始终包含：

- `camera_to_world`：16 个 row-major 值的 4×4 矩阵。它将 UE 相机局部行向量坐标
  变换到世界坐标；世界和相机轴均为 `X forward / Y right / Z up`，平移单位为米。
- `projection_mode`：UE 的投影模式枚举。

能调用游戏专用 `GSE_EngineFuncLib` 时，sample 还包含：

- `world_to_clip`：引擎直接返回的、row-major 的 4×4 World-to-Clip 矩阵。
- `viewport_px`：对应投影的游戏视口 `[width, height]`。

`world_to_clip` 保持引擎原样，因此它的世界点输入单位是 UE 厘米；要与米制 C2W 或
米制世界点结合时，先将位置乘以 100。每个 raw header 都明确记录这两种单位。

`world_to_clip` 不可用时仍会保留精确外参，并以
`projection_status: "unavailable"` 标识降级；不会写入可由矩阵求逆得到的 inverse-VP，
也不重复写入已包含在外参中的位置、欧拉角或轴向量。

完整 Windows 场景验收步骤见 `WINDOWS_VALIDATION.md`。

## 参考包验证基线

- 游戏版本：`1.0.21.23831`
- Build ID：`21393610`
- 游戏 exe 字节数：`728458376`
- UE4SS：`3.0.1`，参考 SHA：`bbdd918`

这些数据是本 payload 的参考包验证基线，不代表对其他游戏版本的兼容性承诺。
静态文件的 SHA256 与字节数见 `payload-manifest.json`。

## 第三方许可

`dwmapi.dll`、`UE4SS.dll` 及相关 UE4SS 配置来自参考包。发布或再分发前必须核实
其来源、许可证、署名和二进制再分发条件；详见 `NOTICE.md`。
