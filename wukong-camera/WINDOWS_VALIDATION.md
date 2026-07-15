# Windows 端到端验证

前置条件：使用离线单机模式，关闭两款游戏后运行根目录 `install.bat`，确认 GTA 与
黑神话安装结果。黑神话版本不匹配时先取消，不要在正式采集机上直接强制。

每个场景录制 10 秒，然后检查 session：

1. 只启动 GTA V：应生成 `camera.jsonl`，`meta.camera.source` 为
   `gta_scripthook_gameplay_cam`，不应残留 `camera_raw_wukong.jsonl`。
2. 只启动黑神话：应生成 `camera.jsonl`，`meta.camera.source` 为
   `wukong_ue4ss_camera_cache`，schema 为 `wukong_camera_v1`。
3. 两款游戏都不启动：视频和输入正常保存，不生成 `camera.jsonl`，CPU 占用不应出现
   游戏插件进程。
4. 两款游戏同时启动并都产生日志：不生成混合 `camera.jsonl`；
   `meta.camera.status` 为 `conflict`，两份 raw 均保留。
5. 黑神话 exe 字节数不匹配：交互安装必须输入大写 `YES` 才继续；无人值守且未传
   `--force-version` 时应失败，但根安装器仍继续完成录制器安装。
6. 安装前放置测试用原有 UE4SS/`xinput1_3.dll`，安装后确认旧加载器被备份且暂时移除；
   运行 `wukong-camera\uninstall.bat` 后应完整恢复。

对成功的单源 session 执行：

```bat
uv run python -c "import json,pathlib; p=pathlib.Path(r'recordings\SESSION'); m=json.loads((p/'meta.json').read_text(encoding='utf-8')); c=[json.loads(x) for x in (p/'camera.jsonl').read_text(encoding='utf-8').splitlines()]; t=[json.loads(x) for x in (p/'frame_timestamps.jsonl').read_text(encoding='utf-8').splitlines()]; print(m['camera']); print(len(c),len(t),c[:1],c[-1:])"
ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nw=1 recordings\SESSION\VIDEO.mp4
```

验收：

- `camera.jsonl` 的 `frame` 严格递增且都存在于 `frame_timestamps.jsonl`。
- 每行仅含基础相机字段及对齐字段：`pos`、`rot`、`fov`、`t_unix_ms`、`frame`、
  `t_capture_unix_ms`、`dt_ms`。
- `abs(dt_ms) <= 50`；黑神话位置单位为米，旋转顺序为 `[pitch, roll, yaw]`。
- `camera.jsonl` 行数允许少于 MP4 帧数（启动/停止边界或超阈值帧会缺失），但不应为 0。
- 停止录制后 `.gta_camera/active_session.json` 与
  `.wukong_camera/active_session.json` 都应为 `idle`。
