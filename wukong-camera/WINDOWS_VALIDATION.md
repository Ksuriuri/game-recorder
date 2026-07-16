# Windows 端到端验证

前置条件：使用离线单机模式，关闭两款游戏后运行根目录 `install.bat`，确认 GTA 与
黑神话安装结果。黑神话版本不匹配时先取消，不要在正式采集机上直接强制。

每个场景录制 10 秒，然后检查 session：

1. 只启动 GTA V：应生成 `camera.jsonl`，`meta.camera.source` 为
   `gta_scripthook_gameplay_cam`，不应残留 `camera_raw_wukong.jsonl`。
2. 只启动黑神话：应生成 `camera.jsonl`，`meta.camera.source` 为
   `wukong_ue4ss_camera_cache`，schema 为 `wukong_camera_v2`。
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
- GTA 每行含 `camera_to_world`（16 个 row-major 值）、`fov_vertical_deg`、
  `viewport_px` 及对齐字段；只应在 gameplay camera 渲染时出现样本。
- 黑神话每行含 `camera_to_world`（16 个 row-major 值）、`projection_mode` 及对齐
  字段。正常情况下还应有 16 元 `world_to_clip` 与 `viewport_px`；若游戏专用投影函数
  不可用，则必须保留外参并标记 `projection_status: "unavailable"`。
- `abs(dt_ms) <= 50`；黑神话 C2W 的世界和相机轴为 `X forward / Y right / Z up`，
  平移单位为米；原始 `world_to_clip` 的世界点输入单位仍为 UE 厘米。
- `camera.jsonl` 行数允许少于 MP4 帧数（启动/停止边界或超阈值帧会缺失），但不应为 0。
- 停止录制后 `.gta_camera/active_session.json` 与
  `.wukong_camera/active_session.json` 都应为 `idle`。

## 几何抽检

对两款游戏各选一段相机静止、随后沿一个已知方向平移的短录制：

1. 取对应 `camera_to_world` 的最后四个元素 `[M41, M42, M43, M44]`。GTA 应与游戏中
   已知的相机位置（米）一致；黑神话的前三项乘以 100 后，应与 UE POV 的厘米坐标一致。
   静止帧的平移应稳定，平移时只沿预期的世界轴变化。
2. 黑神话若有 `world_to_clip`，从调试工具取得一个可见静态世界点的 UE 厘米坐标
   `[x, y, z, 1]`，按行向量计算 `clip = point × world_to_clip`，再以
   `ndc = clip.xyz / clip.w` 转换为像素
   `((ndc.x + 1) * width / 2, (1 - ndc.y) * height / 2)`。结果应落在该物体的视频像素
   附近；若偏差持续存在，保留 raw、`meta.json` 和视频以排查 viewport/游戏版本差异。
3. 检查黑神话 raw 和最终 `camera.jsonl` 均没有 `pos`、`rot`、`fov`、轴向量或 inverse-VP；
   检查 GTA 没有 `pos`、`rot`、`forward` 或玩家位置等重复位姿字段。
