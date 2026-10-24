# 发布审查报告 — astrbot_plugin_bili_scroll 0.13.0

审查日期：2026-10-24  
审查范围：源码、配置、WebUI、测试、发布包、live 部署、GitHub 发布、许可与署名。  
结论：**0.13.0 新增 B 站私信逐条分享，已通过本地审查与 live 验证；GitHub tag/Release/CI 随后发布。**

## 0.13.0 新增：B 站私信逐条分享

- `dm_share_mode=single`（默认）：每条挑中的视频单独发一条私信，优先尝试 **B 站原生视频分享卡片**（`msg_type=7`，标题/封面/UP/跳转）。
- **实测**：B 站 web `send_msg` 对 `msg_type=7` 返回 `-400 请求错误`（接口只承诺 1/2/5）；自动退回**自然口吻文字**（理由 + 标题 + BV 链接）并发送成功，不再使用 `【B站分享】` 固定模板；首个失败后缓存「卡片不可用」，后续直接发文字。
- **live 实发验证**：向配置的 `dm_uid` 发送一条测试私信，卡片返回 `-400`；文字兜底返回成功，随后用 `fetch_session_msgs` 确认目标会话里能看到这条文字（类型 1，内容为理由 + 标题 + BV 链接）。
- 防刷屏：跳过当天已分享的 BV；每轮最多 `dm_share_limit=3` 条、每天最多 `dm_share_daily_limit=10` 条、两条间隔 `dm_share_interval=5` 秒；发送失败不重试。
- `dm_share_mode=digest` 保留旧的汇总一条行为。
- 控制台新增日报投递、分享方式、接收 UID、每轮/每日上限、间隔设置。
- 注意：B 站禁止给自己发私信（21026），`dm_uid` 必须是另一个账号的 UID；`dm_mode` 默认仍为 off。

## 通过项

| 项目 | 结果 |
|---|---|
| 全量单测（有 AstrBot 源） | 163 passed |
| 干净环境单测（无 AstrBot 源） | 108 passed / 1 skipped |
| Python 编译 | `compileall` 通过 |
| 发布包结构 | 干净解压，必需模块齐全，`import main` + `routes=12` 成功 |
| 发布包扫描 | 禁名 0、绝对路径 0、凭据 0 |
| schema / README 一致性 | 57 键全部有文档，无过期表述 |
| WebUI | JS 语法通过，97 个元素 ID、93 个引用全部存在 |
| live 部署 | 0.13.0 已加载，`dm_share_mode=single` / 3 / 10 / 5 正常读出 |

发布包：`release/astrbot_plugin_bili_scroll-0.13.0.zip`  
本地构建：108,714 B，SHA256 `fbcae47c805cf80f1157ec9f4216348b42a9e411063406e3c92d82b268ff4603`

## 发布状态

- 仓库：<https://github.com/SIiOC/astrbot-plugin-bili-scroll>（public，MIT）
- 待推送：`v0.13.0` tag；Release 工作流已改为按 tag 自动构建 zip + sha256
- 历史：0.12.2 的 CI/Release 正常；0.12.1 CI 因漏声明 aiohttp 失败，**不要用 0.12.1 安装包**

## 本轮修复

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
12. **漏声明 `aiohttp`**：`bili_client.py` 直接 import aiohttp，但 requirements 未声明；0.12.2 补上 `aiohttp>=3.8`，CI 转绿。
13. **私信逐条分享**：新增 `build_dm_share_card` / `build_dm_share_fallback_text` / `select_dm_share_items`；卡片含标题/封面/UP/跳转，失败自动转自然文字，含当天去重、每日上限、间隔与失败不重试。

## 仍需处理

| 优先级 | 事项 | 状态 |
|---|---|---|
| P1 | README 截图/市场展示图 | 未处理 |
| P1 | AstrBot 插件市场提交 | 未处理，需市场账号 |
| P1 | `comment_blocklist` 为空 | 需填入真实姓名/别名/单位等禁词 |
| P1 | 私信接收账号 | 需在页面填 `dm_uid`（另一个账号）并把 `dm_mode` 改为 only/both 才会发送 |
| P2 | `bilibili-api-python` 为 GPLv3+ 运行时依赖 | 已在 NOTICE 披露，建议发布前做许可确认 |
| P2 | 旧 `0.12.0` / `0.12.1` 包 | 0.12.0 缺 vision.py；0.12.1 缺 aiohttp 依赖声明；**只用 0.13.0** |

## 个人信息扫描

- **0.13.0 发布包**：新增 `dm_share_*` 配置与页面字段，均为空默认值/纯配置键；未发现主人 B 站昵称、QQ/WebChat 标识、手机号、身份证、邮箱、Windows 本地路径或实际凭据值。
- **源码**：测试夹具里的 `Ivy` 已替换为 `guest`；`13800138000`、`a@b.com`、`1790000000` 是合成占位数据。
- **live 运行数据**：`bili_credential.json`、`state.json`（B站昵称/QQ 会话/最近挑中）、`sent.jsonl`、`out/` 日报含个人信息，位于 AstrBot `data/plugin_data/astrbot_plugin_bili_scroll/`，**不在发布包与源码中**。不要公开备份整个 `plugin_data` 或含这些字段的截图。

## 建议后续

1. 推送 `v0.13.0` tag，由 Release 工作流生成 zip + sha256。
2. 在页面填 `dm_uid`，把 `dm_mode` 改成 `only` 或 `both`，验证逐条私信。
3. 补页面截图与市场文案，提交 AstrBot 插件市场。
4. 填入 `comment_blocklist`，确认 GPL 运行时依赖的许可兼容性。
