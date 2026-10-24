# 发布审查报告 — astrbot_plugin_bili_scroll 0.12.2

审查日期：2026-10-24  
审查范围：源码、配置、WebUI、测试、发布包、live 部署、GitHub 发布、许可与署名。  
结论：**0.12.2 已通过审查并发布到 GitHub（repo + tag + Release 附件 + CI 绿）；仍缺截图与插件市场提交。**

## 通过项

| 项目 | 结果 |
|---|---|
| 全量单测（有 AstrBot 源） | 157 passed |
| 干净环境单测（无 AstrBot 源） | 105 passed / 1 skipped |
| GitHub Actions CI | 0.12.2 成功（0.12.1 因漏声明 aiohttp 失败） |
| Python 编译 | `compileall` 通过 |
| 发布包结构 | 干净解压，必需模块齐全，`import main` + `routes=12` 成功 |
| 发布包扫描 | 禁名 0、绝对路径 0、凭据 0 |
| schema / README 一致性 | 53 键全部有文档，无过期表述 |
| WebUI | JS 语法通过，87 个元素引用均有对应 ID |
| live 部署 | 0.12.2 已加载，页面 API 12、LLM 工具 1、状态正常 |
| live 与源码 | 关键文件 SHA256 完全一致 |
| 评论复核 live 冒烟 | 预览生成成功，未发公开评论 |

发布包：`release/astrbot_plugin_bili_scroll-0.12.2.zip`  
本地构建：104,517 B，SHA256 `7b6874b051e37d39bef22f306682ff6aca9a8163b34193a92ba4d047be7672a6`  
GitHub Release 附件：104,512 B，SHA256 `d89e1e52c2645269ae8842fdf449794d4eb36dad82c9acc9a52c6ccf6ed29278`（CI 构建，已解压验证 `import main` 成功）

## 发布状态

- 仓库：<https://github.com/SIiOC/astrbot-plugin-bili-scroll>（public，MIT）
- 分支：`main` = `ce29d1c`
- tag：`v0.12.2` = `ce29d1c`
- CI：<https://github.com/SIiOC/astrbot-plugin-bili-scroll/actions/runs/40064096783>（success）
- Release：<https://github.com/SIiOC/astrbot-plugin-bili-scroll/releases/tag/v0.12.2>（zip + sha256 已上传并校验）
- 0.12.1 的 tag 与 CI 运行保留，但其 CI 因依赖声明缺失失败，**不要用 0.12.1 安装包**

## 本轮发现并修复

1. **发布包漏 `vision.py`**：旧白名单缺失，解压后 `import main` 直接失败；已补入白名单并重建。
2. **验收脚本选错包**：按文件名字符串排序，`0.9.0` 排在 `0.12.0` 后面；已改为按语义版本取最新，并增加必需源码模块检查。
3. **群聊隐私**：发送记录注入与 `bili_sent_history` 工具原先只校验会话 umo，群成员可问出记录；现只对主人私聊生效。
4. **兜底评论绕过复核**：人设关闭/不可用/生成失败时返回的兜底评论未走发送前复核；现已纳入。
5. **复核 JSON 布尔漏洞**：字符串 `"false"` 会被 `bool()` 当真；现只接受 JSON 布尔 `true`，其余 fail closed。
6. **禁词泄漏给模型**：复核 prompt 原先拼接 `comment_blocklist`（可能含真实姓名）；现已移除，仅本地过滤。
7. **PII 误伤**：账号数字正则原为 5–12 位，正常播放量/年份会被拦；现收紧到 8–12 位，并收窄「不如…」拉踩正则。
8. **字幕 SSRF 面**：字幕 CDN URL 未校验；现只允许 https 公网地址。
9. **视觉配置夹取**：MB/帧数/秒数统一夹到安全范围。
10. **文件无限增长**：`seen.md` 保留最近 2 万条、`sent.jsonl` 保留最近 5000 条；@昵称清洗空白和长度。
11. **测试时间依赖**：夜间 quiet 时段与硬编码日期已改为动态日期。
12. **漏声明 `aiohttp`**：`bili_client.py` 直接 import aiohttp，但 requirements 未声明；干净环境/CI `ModuleNotFoundError`。0.12.2 补上 `aiohttp>=3.8`，CI 转绿。

## 仍需处理

| 优先级 | 事项 | 状态 |
|---|---|---|
| P1 | GitHub Release 附件 | 已完成：zip + sha256 已上传，SHA256 校验与解压 import 均通过 |
| P1 | README 截图/市场展示图 | 未处理 |
| P1 | AstrBot 插件市场提交 | 未处理，需市场账号 |
| P1 | `comment_blocklist` 为空 | 需填入真实姓名/别名/单位等禁词 |
| P2 | `bilibili-api-python` 为 GPLv3+ 运行时依赖 | 已在 NOTICE 披露，建议发布前做许可确认 |
| P2 | 旧 `0.12.0` / `0.12.1` 包 | 0.12.0 缺 vision.py；0.12.1 缺 aiohttp 依赖声明；**只用 0.12.2** |

## 个人信息扫描

- **0.12.2 发布包**：新增内容仅为依赖声明、版本和 CHANGELOG；扫描全部文本条目，未发现主人 B 站昵称、QQ/WebChat 标识、手机号、身份证、邮箱、Windows 本地路径或实际凭据值。
- **源码**：测试夹具里的 `Ivy` 已替换为 `guest`；`13800138000`、`a@b.com`、`1790000000` 是合成占位数据。
- **live 运行数据**：`bili_credential.json`、`state.json`（B站昵称/QQ 会话/最近挑中）、`sent.jsonl`、`out/` 日报含个人信息，位于 AstrBot `data/plugin_data/astrbot_plugin_bili_scroll/`，**不在发布包与源码中**。不要公开备份整个 `plugin_data` 或含这些字段的截图。

## 建议后续

1. 在 GitHub 仓库创建 Release，附上 `release/astrbot_plugin_bili_scroll-0.12.2.zip` 与 SHA256。
2. 补页面截图与市场文案，提交 AstrBot 插件市场。
3. 页面填入 `comment_blocklist`。
4. 确认 GPL 运行时依赖的许可兼容性。
