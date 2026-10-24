# NOTICE

本项目（`astrbot_plugin_bili_scroll`）是面向 AstrBot 的独立 Python 实现，
在功能设计与交互流程上参考了以下开源项目。**本项目未复制上述项目的源代码**，
参考内容仅涉及公开可见的行为、流程与交互设计。

## 参考项目

- **dsh-bili-scroll** — https://github.com/c1overzz/dsh-bili-scroll
  - License: MIT
  - 参考内容：扫码登录、关键词搜索、读字幕判断、日报与评论 @ 的交互与流程设计
- **bilibili-mcp** — https://github.com/AGI_Ananas/bilibili-mcp
  - License: MIT（Copyright (c) 2026 AGI_Ananas）
  - 上游项目曾作为 B 站接口思路的参考；本项目未分发其代码

## 调研记录（未使用代码）

- **dsh-bili-humanism** — https://github.com/InariNGT-Ofiicial/dsh-bili-humanism
  - License: MIT
  - 2026-10 调研其 B 站内容阅读、手账与人格化流程；其定位是内容学习/人格积累，与本项目“自动刷选+日报/评论通知”的交互不同，本项目未采用其代码或核心设计。

## 运行时依赖

- **bilibili-api-python** — https://github.com/Nemo2011/bilibili-api
  - License: GPLv3+
  - 仅作为运行时依赖通过 pip 安装，不随本项目分发

如上述署名有遗漏或需要调整，请通过仓库 issue 联系维护者。
