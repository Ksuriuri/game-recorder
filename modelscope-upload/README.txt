ModelScope 录制数据上传包
========================

将本文件夹放在 game-recorder 项目根目录下（与 recordings、install.bat 同级）。

一键上传
--------
  详见 使用方式.txt

  双击 upload.bat
  - 首次会自动安装 modelscope（离线包用 wheels\，否则联网安装）
  - 上传 ../recordings/ 下各 session 到数据集内的 recordings/ 子目录
  - 远程已有同名文件夹则跳过

目录结构（解压后）
------------------
  game-recorder/
    recordings/              <- 录制数据（本工具读取这里）
    install.bat              <- 需先安装过录制器
    .tools/                  <- 复用录制器的 uv / Python
    modelscope-upload/
      upload.bat             <- 一键安装 + 上传
      install.bat            <- 仅安装上传环境
      upload_recordings.py
      wheels/                <- 离线 wheel（打包时生成）

制作离线 zip（开发机，需联网）
------------------------------
  1. 确保项目根目录已运行过 install.bat
  2. 双击 modelscope-upload\build_bundle.bat
  3. 得到项目根目录下的 modelscope-upload-portable-YYYYMMDD.zip
  4. 在网吧等机器：解压 zip 到 game-recorder 根目录，双击 upload.bat

配置
----
  修改 upload_recordings.py 顶部：
    DEFAULT_REPO_ID
    MODELSCOPE_TOKEN
