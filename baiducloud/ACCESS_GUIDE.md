# 百度网盘数据访问快速上手

本目录通过百度网盘开放平台 OAuth 访问**当前这份文档对应的百度账号**网盘数据，
用于读取目录、统计 `/game-data` 录制会话，以及检查 `camera.jsonl` 等附加文件。

拿到的不是“你自己的网盘”，而是这份凭据已授权的那份账号数据。

## 别人上手最短路径

1. 拿到本仓库（或至少拿到 `baiducloud/` 目录）。
2. 安装 [uv](https://docs.astral.sh/uv/) 与 Python 3.10+。
3. 在 `baiducloud/` 下按下文创建 `keys.txt` 和 `token.json`。
4. 在**项目根目录**跑冒烟测试：

```bash
uv run --no-project python baiducloud/list_files.py --dir /game-data
```

看到会话目录列表即表示可用。通常**不必重新走 OAuth**；复制令牌即可。

注意：`keys.txt` / `token.json` 被 `.gitignore` 忽略。从 Git 克隆后这两个文件不会出现，
必须按本文手动创建。

## 需要哪些文件

最小可用集合：

| 文件 | 是否必需 | 说明 |
| --- | --- | --- |
| `list_files.py` | 必需 | 授权、列目录、令牌刷新 |
| `analyze_game_data.py` | 统计时必需 | 依赖同目录下的 `list_files.py` |
| `keys.txt` | 必需 | 应用凭据；Git 不会带过来 |
| `token.json` | 推荐 | 已有访问令牌；没有则需重新授权 |
| `ACCESS_GUIDE.md` | 可选 | 本文档 |
| `README.md` | 可选 | 更完整的说明与历史统计 |

脚本只使用 Python 标准库，不需要 `pip install`。

## 本地凭据

### 应用凭据：创建 `baiducloud/keys.txt`

格式必须是「名称一行、值一行」，内容如下（可整段复制）：

```text
AppID
123949723
AppKey
4FFcAk3syKdpdRkeC1tW1m5dHezNbZpX
Secretkey
eiROaEoOA6VpFtuoWQ6lJ2JW0NtsHGtm
Signkey
D~wze=F*UbAbSmqHSj=+f=lfLD$+LMWp
```

当前脚本实际用到的是 `AppKey` 与 `Secretkey`；`AppID` / `Signkey` 一并保留即可。

### OAuth 令牌：创建 `baiducloud/token.json`

截至 2026-07-19 可用的令牌（可整段复制）：

```json
{
  "expires_in": 2592000,
  "refresh_token": "122.b4d9a55272bdd6f73f8b2633d0d50b8c.YgS4-X5zyevnkSrOko6A4KxXMXeMe03Uo5rZxu8.s7uaOg",
  "access_token": "121.94766ea274eda397478dd1af6f365841.YHqS7coSXZ83eVnWT7fN2iqlHFGRHEJl980Pp7S.lk3UDA",
  "session_secret": "",
  "session_key": "",
  "scope": "basic netdisk",
  "obtained_at": 1783755959
}
```

access token 过期后，脚本会用 refresh token 自动刷新并回写 `token.json`。
若本地文件已更新，以本地为准。

## 环境与工作目录

推荐在**项目根目录**执行：

```bash
uv run --no-project python baiducloud/list_files.py --help
```

`analyze_game_data.py` 通过 `from list_files import ...` 导入同目录模块，
因此也可以先 `cd baiducloud` 再执行：

```bash
uv run --no-project python analyze_game_data.py
```

## 令牌失效时再授权

仅在列目录报错、或提示缺少 / 无法刷新令牌时，才需要重新授权：

1. 确认 `keys.txt` 已就位。
2. 打开授权页：

   ```bash
   uv run --no-project python baiducloud/list_files.py --authorize
   ```

3. 浏览器登录**同一百度账号**，同意 `basic,netdisk` 权限，拿到一次性 Authorization Code。
4. 换取令牌：

   ```bash
   uv run --no-project python baiducloud/list_files.py --code "AUTHORIZATION_CODE"
   ```

授权回调使用 `redirect_uri=oob`（页面直接显示授权码）。
若百度开放平台控制台要求配置回调地址，需允许 `oob`，或与脚本保持一致。

## 读取网盘目录

列出根目录：

```bash
uv run --no-project python baiducloud/list_files.py
```

列出录制数据根目录：

```bash
uv run --no-project python baiducloud/list_files.py --dir /game-data
```

递归列出所有内容：

```bash
uv run --no-project python baiducloud/list_files.py --dir /game-data --recursive
```

递归读取会逐目录调用网盘接口；当前数据量较大，运行数分钟属于正常现象。

## 统计录制时长

执行完整刷新：

```bash
uv run --no-project python baiducloud/analyze_game_data.py
```

脚本会：

1. 递归扫描 `/game-data`，不依赖可能漏结果的网盘搜索接口。
2. 只下载每个会话很小的 `meta.json`，不下载 MP4 或 JSONL 内容。
3. 只将同时具备 `meta.json` 与 `.mp4` 的会话纳入时长。
4. 使用 `meta.json.duration_s` 作为视频时长。
5. 写入完整结果到 `baiducloud/game_data_report.json`。

快速重跑（复用此前报告的元数据，但仍重新核验目录和 MP4 是否存在）：

```bash
uv run --no-project python baiducloud/analyze_game_data.py --use-cache
```

报告包含：

- `totals`：录制员数、有效会话数、总时长和数据质量计数。
- `daily`：按日期聚合的会话数、时长和录制员细分。
- `recorders`：按录制员聚合的累计数据。
- `sessions`：逐会话明细。
- `data_quality`：缺少 MP4、缺少 meta、下载失败等异常。

## 录制数据格式

每个录制会话通常为：

```text
/game-data/{recording_id}_session_{YYYYMMDD_HHMMSS}/
  {recording_id}_{YYYYMMDD_HHMMSS}_{start_frame}_{end_frame}.mp4
  {recording_id}_{YYYYMMDD_HHMMSS}_{start_frame}_{end_frame}.jsonl
  meta.json
  camera.jsonl                 # 可选
```

| 文件 | 作用 |
| --- | --- |
| `meta.json` | 权威元数据，含 `duration_s`、`fps`、`total_frames`、`segments` 等。 |
| `*.mp4` | 实际视频文件。 |
| `*.jsonl` | 稀疏输入事件流；不能以最后一行推算视频总时长。 |
| `camera.jsonl` | 可选的相机数据日志。 |

录制日期取 `meta.json.session_timestamp`；时长取 `meta.json.duration_s`。

## 检查 camera.jsonl

可使用下面的只读脚本模板扫描指定日期。它只读取目录项，不下载视频：

```bash
cd baiducloud
uv run --no-project python - <<'PY'
from concurrent.futures import ThreadPoolExecutor, as_completed
from list_files import list_directory, load_keys, load_token

token = load_token(load_keys())["access_token"]
for entry in list_directory(token, "/game-data"):
    if not entry.get("isdir") or "20260719" not in entry["path"]:
        continue
    files = list_directory(token, entry["path"])
    if any(item.get("server_filename", "").lower() == "camera.jsonl" for item in files):
        print(entry["path"])
PY
```

对于跨日期、时长或录制员的正式统计，优先扩展 `analyze_game_data.py`，以保持
“有 MP4 + 有 meta.json 才计入”的数据质量规则。

## 收集文件任务

百度网盘“收集文件”创建后，收集任务区不一定作为普通网盘目录出现在开放 API 中。
启用自动转存，或手动转存到普通网盘目录后，脚本才能读取。建议将已收集文件整理到：

```text
/game-data/{录制会话目录}
```

不要将整个收集任务父目录嵌套到一个 session 目录中，否则递归统计可能将内部
子会话识别为缺失 MP4 的不完整记录。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `缺少本地令牌` / `keys.txt` 不存在 | 按上文创建文件；Git 克隆不会带出凭据 |
| `授权失败` / `刷新令牌失败` | 重新走 `--authorize` + `--code`；确认登录的是同一百度账号 |
| `HTTP 401/403` | 令牌失效或 scope 不足；重新授权并确保包含 `netdisk` |
| 搜索结果为空，但网页能看到文件 | 不要依赖网盘 search；用 `list_files.py --dir` 或 `analyze_game_data.py` 的递归列举 |
| 统计很慢 | 正常：会遍历大量会话目录并下载所有 `meta.json` |
| 只有收集任务区有文件，API 读不到 | 先转存到普通网盘目录（如 `/game-data`） |
| `ModuleNotFoundError: list_files` | 在 `baiducloud/` 下运行，或从项目根用 `python baiducloud/...` |
