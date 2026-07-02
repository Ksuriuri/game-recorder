ModelScope 数据工具包
====================

包含抽样下载与过小数据清理两个工具。详见 使用方式.txt。

一键使用
--------
  sample.bat    按日期抽样下载视频到 data\（可自定义每人抽样 1-20 条，默认 5）
  cleanup.bat   删除 ModelScope 上 mp4 总大小 < 10MB 的 session

目录结构（解压后）
------------------
  modelscope-sample/
    sample.bat                 <- 抽样下载
    cleanup.bat                <- 清理过小数据
    install.bat                <- 仅安装环境
    sample_recordings.py
    cleanup_short_sessions.py
    wheels/                    <- 离线 wheel（打包时生成）
    .tools/                    <- 便携 uv / Python（打包时生成）
    data/                      <- 抽样输出（运行时生成）
    .cache/                    <- 缓存（运行时生成）

制作离线 zip（开发机，需联网）
------------------------------
  1. 确保 game-recorder 根目录已运行过 install.bat
  2. 双击 modelscope-sample\build_bundle.bat
  3. 得到 modelscope-sample-portable-YYYYMMDD.zip
  4. 解压到任意目录后双击 sample.bat 或 cleanup.bat

  cleanup.bat 还需目标电脑安装 Git（含 Git LFS）。

配置
----
  修改脚本顶部常量：
    sample_recordings.py       DEFAULT_REPO_ID, MODELSCOPE_TOKEN
    cleanup_short_sessions.py  DEFAULT_REPO_ID, MODELSCOPE_TOKEN
