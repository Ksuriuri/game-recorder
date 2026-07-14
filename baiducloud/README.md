# 百度网盘读取与录制时长统计

本目录包含百度网盘 OAuth 授权、文件列表读取，以及 `/game-data`
录制数据统计工具。统计过程只下载小型 `meta.json`，不会下载或读取 MP4 视频内容。

## 文件说明

- `list_files.py`：OAuth 授权、令牌刷新、目录及文件列表读取。
- `analyze_game_data.py`：统计 `/game-data` 的录制员、会话和每日视频时长。
- `game_data_report.json`：最近一次统计生成的完整机器可读报告。
- `keys.example.txt`：开放平台应用凭据格式模板。
- `keys.txt`：实际应用凭据，不纳入 Git。
- `token.json`：OAuth access token 与 refresh token，不纳入 Git。
- `.gitignore`：保护凭据、令牌和生成报告。

脚本只使用 Python 标准库，无需额外安装依赖。

## 首次配置与授权

1. 复制 `keys.example.txt` 为 `keys.txt`，填入百度网盘开放平台应用信息。
2. 打开 OAuth 授权页：

   ```bash
   uv run --no-project python baiducloud/list_files.py --authorize
   ```

3. 登录百度账号并允许访问，将页面显示的一次性 Authorization Code 换取令牌：

   ```bash
   uv run --no-project python baiducloud/list_files.py --code "AUTHORIZATION_CODE"
   ```

令牌写入 `baiducloud/token.json`，文件权限会设置为仅当前用户可读写。
后续运行不需要再次提供授权码；访问令牌过期时会使用 refresh token 自动刷新。

## 读取云盘文件

读取根目录：

```bash
uv run --no-project python baiducloud/list_files.py
```

读取 `/game-data`：

```bash
uv run --no-project python baiducloud/list_files.py --dir /game-data
```

递归读取 `/game-data`：

```bash
uv run --no-project python baiducloud/list_files.py --dir /game-data --recursive
```

递归读取会对每个目录调用一次列表接口，目录较多时需要数分钟。

## 统计录制时长

获取最新云端数据并重新下载所有 `meta.json`：

```bash
uv run --no-project python baiducloud/analyze_game_data.py
```

复用上次报告中的元数据，只重新核验云端 MP4 是否存在：

```bash
uv run --no-project python baiducloud/analyze_game_data.py --use-cache
```

结果写入 `baiducloud/game_data_report.json`，其中包含：

- `totals`：录制员、视频会话、总时长和数据质量计数。
- `daily`：按日期汇总，并包含每名录制员的时长。
- `recorders`：每名录制员的累计会话和时长。
- `data_quality`：缺失视频、缺失元数据及接口失败记录。
- `sessions`：每个会话的明细。

## 录制文件保存格式

默认输出根目录为 `recordings/`。每次录制创建一个 session 目录：

```text
{recording_id}_session_{YYYYMMDD_HHMMSS}/
  {recording_id}_{YYYYMMDD_HHMMSS}_{start_frame}_{end_frame}.mp4
  {recording_id}_{YYYYMMDD_HHMMSS}_{start_frame}_{end_frame}.jsonl
  meta.json
```

- `meta.json` 是会话级权威元数据，包含 `duration_s`、`fps`、
  `total_frames`、`segments`、`auto_stop_reason` 等字段。
- `.jsonl` 是稀疏输入事件流，每行按视频帧记录 `frame` 与 `events`；
  无事件的帧不会写入，因此不能用最后一行估算视频时长。
- `.mp4` 是实际录屏视频；本统计只检查文件是否存在，不读取视频内容。
- 视频时长取 `meta.json.duration_s`，它由裁剪后的 `total_frames / fps`
  计算并保留两位小数。
- 日期取 `meta.json.session_timestamp` 的日期部分。

## 录制员口径

项目本身将完整的 `--recording-id` 写入 session 名称。当前云端命名还在末尾附加了日期，
例如 `HYDBK-LZ07-20260710`。

报告同时保留两种信息：

- `recording_id`：完整原始 ID；当前共有 8 个。
- `recorder`：移除末尾 `-YYYYMMDD` 后的基础 ID；当前归并为 4 名录制员。

日期以 `session_timestamp` 为准。当前存在 5 个
`HYDPK2-LZ01-20250709` 会话，其实际 session 日期是 2026-07-09，
因此报告归入 2026-07-09。

## 2026-07-11 当前统计

数据范围：2026-07-07 至 2026-07-10。

- 归并录制员：4 名。
- 原始 recording ID：8 个。
- 已下载并解析的 `meta.json`：187 个。
- 实际包含云端 MP4 的会话：179 个。
- 视频总时长：110651.72 秒，即 30 小时 44 分 12 秒。
- 2026-07-07：14 个会话，4 小时 5 分 34 秒。
- 2026-07-09：36 个会话，7 小时 0 分 13 秒。
- 2026-07-10：129 个会话，19 小时 38 分 25 秒。
- 8 个会话只有 `meta.json`、没有 MP4，均为
  `HYDPK2-LZ01` 的 2026-07-10 数据，未计入视频时长。
- 未发现视频列表读取失败、元数据下载失败或解析失败。

## 安全注意事项

- 不要提交或分享 `keys.txt`、`token.json`。
- 不要把 access token、refresh token、Secret Key 输出到日志。
- 若凭据泄露，应在百度开放平台重置应用密钥并撤销用户授权。
