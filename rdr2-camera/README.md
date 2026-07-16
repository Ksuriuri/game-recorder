# RDR2 原生相机插件

`RDR2CameraPoseLogger.asi` 是 ScriptHookRDR2 的 x64 原生插件。它跟随
game-recorder 发布的 `.rdr2_camera/active_session.json`，把最终渲染相机写入录制会话下的
`camera_raw_rdr2.jsonl`。插件不提供线上模式功能，仅用于 Story Mode。

## 构建

要求：

- Visual Studio 2022 C++ x64 工具集（MSVC v143）；
- 从 [ScriptHookRDR2 官方页面](http://www.dev-c.com/rdr2/scripthookrdr2/) 获取的 SDK；
- 将 SDK 解压到仓库外。不要把 SDK 的 `inc`、`lib` 或 runtime 文件加入本仓库。

构建命令：

```bat
rdr2-camera\build.bat "C:\SDK\ScriptHookRDR2_SDK"
```

也可显式传入 MSBuild：

```bat
rdr2-camera\build.bat "C:\SDK\ScriptHookRDR2_SDK" "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe"
```

参数为空时，脚本分别读取 `SDK_ROOT`、`MSBUILD` 环境变量，并尝试用 PATH 或
Visual Studio Installer 定位 MSBuild。项目只定义 `Release|x64`，使用静态 MSVC runtime
（`/MT`），产物位于
`rdr2-camera\CameraPoseLogger\bin\Release\RDR2CameraPoseLogger.asi`。

## Story Mode 安装和使用

1. 按 ScriptHookRDR2 官方说明安装当前游戏版本适用的 runtime/ASI loader。不要从本仓库获取
   或分发这些官方文件。
2. 把构建出的 `RDR2CameraPoseLogger.asi` 和 `rdr2_camera.config.json` 放入 RDR2 游戏根目录
   （与 `RDR2.exe` 同目录）。
3. 编辑配置中的 `control_file`，使它指向 game-recorder 输出目录旁的
   `.rdr2_camera\active_session.json`。例如输出目录是
   `D:\captures\recordings`，控制文件就是
   `D:\captures\.rdr2_camera\active_session.json`。路径支持 `%USERPROFILE%` 等环境变量。
   也可用进程环境变量 `GAME_RECORDER_RDR2_CONTROL` 指定；配置文件中的非空值优先。
4. 启动 RDR2，进入 **Story Mode**，再正常开始/停止 game-recorder。无需插件热键。

不要在 Red Dead Online 中加载 ScriptHookRDR2 或本插件。游戏更新后，如果 ScriptHookRDR2
报告版本不兼容，应从官方页面更新 runtime；不要用旧 runtime 强行启动。

## 控制文件和输出

插件每 100 ms 轮询一次控制文件，并接受 GTA 插件同款字段：

- `status`：`recording` 时开始，其他值时停止；
- `session_id`、`session_dir`、`start_epoch_ms`；
- `sample_hz`：有效范围 1–1000；
- `raw_file`：必须是单个文件名，不能是绝对路径或包含父目录。

控制文件由 game-recorder 原子替换；短暂缺失、共享冲突或不完整 JSON 会在后续轮询重试。
字符串解析支持 JSON 转义和 Unicode surrogate pair。样本时间使用 Windows UTC epoch
milliseconds；每个样本用 `GetClientRect` 读取游戏客户区 viewport。文件开始时写 header，
停止或切换会话时写 footer，默认每 30 个样本 flush 一次。

## 矩阵约定

相机来源是 `GET_FINAL_RENDERED_CAM_COORD`、
`GET_FINAL_RENDERED_CAM_ROT(2)` 和 `GET_FINAL_RENDERED_CAM_FOV`。返回的
`(pitch, roll, yaw)` 按 `Rz(yaw) * Rx(pitch) * Ry(roll)` 合成；代码再把三个相机局部
基向量作为 row-vector 矩阵的前三行。

`camera_to_world` 是 16 个数的 row-major、row-vector 齐次矩阵：

```text
[ right.x    right.y    right.z    0
  forward.x  forward.y  forward.z  0
  up.x       up.y       up.z       0
  position.x position.y position.z 1 ]
```

即 `p_world = p_camera * camera_to_world`。世界轴与相机局部轴都记录为
`X right / Y forward / Z up`，平移单位为米。FOV 标记为 vertical，viewport 是客户区宽高；
它们用于近似重建投影，不是游戏内部 projection matrix。
