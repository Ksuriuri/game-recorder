S3 录制数据上传包
==================

将本文件夹放在 game-recorder 项目根目录下（与 recordings、install.bat 同级）。

一键上传
--------
  详见 使用方式.txt

  双击 upload.bat
  - 读取同目录 oss_credentials.json（OSS 密钥，本地放置，勿提交 git）
  - 首次会自动安装 boto3（离线包用 wheels\，否则联网安装）
  - 上传到阿里云 OSS 桶 aws-kelei 的 game-raw-data/
  - OSS 同名 session 对比清单和大小，完整才跳过
  - 完整性检查只读取远程元数据，不下载远程视频
  - 网络波动时自动重试；失败重开后也会补传不完整的 session
  - 录制器默认在会话结束后自动调用本脚本（--session + --success-log）

单 session / 成功日志
--------------------
  python upload_recordings.py --session ..\recordings\session_xxx ^
    --success-log ..\recordings\auto_uploaded.jsonl

  --session       只上传指定 session 文件夹
  --success-log   上传结果追加一行 JSON（uploaded / already_complete / error）

目录结构
--------
  game-recorder/
    recordings/              <- 录制数据（本工具读取这里）
    install.bat              <- 需先安装过录制器
    .tools/                  <- 复用录制器的 uv / Python
    s3-upload/
      upload.bat             <- 一键安装 + 上传
      oss_credentials.json   <- OSS 密钥（本地文件，勿提交 git）
      oss_credentials.example.json  <- 密钥模板
      install.bat            <- 仅安装上传环境
      upload_recordings.py
      wheels/                <- 离线 wheel（可选）

配置
----
  OSS 密钥：复制 oss_credentials.example.json 为 oss_credentials.json 并填入密钥
