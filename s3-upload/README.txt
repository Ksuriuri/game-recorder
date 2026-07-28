S3 录制数据上传包
==================

将本文件夹放在 game-recorder 项目根目录下（与 recordings、install.bat 同级）。

一键上传
--------
  详见 使用方式.txt

  双击 upload.bat
  - 首次会自动安装 boto3（离线包用 wheels\，否则联网安装）
  - 自动检查百度 /game-data/（凭证已内置），完整则跳过
  - 其余上传到桶内 game-data-raw/
  - S3 同名 session 再对比清单和大小，完整才跳过
  - 完整性检查只读取远程元数据，不下载远程视频
  - 网络波动时自动重试；失败重开后也会补传不完整的 session

目录结构（解压后）
------------------
  game-recorder/
    recordings/              <- 录制数据（本工具读取这里）
    install.bat              <- 需先安装过录制器
    .tools/                  <- 复用录制器的 uv / Python
    s3-upload/
      upload.bat             <- 一键安装 + 上传
      install.bat            <- 仅安装上传环境
      upload_recordings.py   <- S3 凭证内置
      baidu_remote.py        <- 百度凭证内置
      wheels/                <- 离线 wheel（可选）

配置
----
  S3：修改 upload_recordings.py 顶部常量
  百度：修改 baidu_remote.py 顶部 DEFAULT_BAIDU_* 常量
  关闭百度检查：python upload_recordings.py --skip-baidu-check
