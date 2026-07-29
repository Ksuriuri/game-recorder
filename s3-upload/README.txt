S3 录制数据上传包
==================

将本文件夹放在 game-recorder 项目根目录下（与 recordings、install.bat 同级）。

一键上传
--------
  详见 使用方式.txt

  双击 upload.bat
  - 自动解压同目录 oss_credentials.zip → oss_credentials.json（OSS 密钥）
  - 首次会自动安装 boto3 / modelscope（离线包用 wheels\，否则联网安装）
  - 自动检查百度 /game-data/（凭证已内置），完整则跳过
  - 自动检查 ModelScope 数据集 recordings/（凭证已内置），完整则跳过
  - 其余上传到阿里云 OSS 桶 aws-kelei 的 game-raw-data/
  - OSS 同名 session 再对比清单和大小，完整才跳过
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
      oss_credentials.zip    <- OSS 密钥包（不进 git，需单独放进本目录）
      install.bat            <- 仅安装上传环境
      upload_recordings.py
      baidu_remote.py
      modelscope_remote.py
      wheels/                <- 离线 wheel（可选）

配置
----
  OSS 密钥：把 oss_credentials.zip 放进本目录即可（upload.bat 会自动解压）
  百度：修改 baidu_remote.py 顶部 DEFAULT_BAIDU_* 常量
  ModelScope：修改 upload_recordings.py / modelscope_remote.py 顶部常量
  关闭百度检查：python upload_recordings.py --skip-baidu-check
  关闭 ModelScope 检查：python upload_recordings.py --skip-modelscope-check
