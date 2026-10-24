# 发布审查报告 — astrbot_plugin_bili_scroll 0.12.1

审查日期：2026-10-23  
审查范围：源码、配置、WebUI、测试、发布包、live 部署、许可与署名。  
结论：**代码与发布包已达到可发布质量；作为对外正式发布仍缺仓库/截图/市场提交等流程项。**

## 通过项

| 项目 | 结果 |
|---|---|
| 全量单测（有 AstrBot 源） | 157 passed |
| CI 形态单测（无 AstrBot 源） | 105 passed / 1 skipped |
| Python 编译 | `compileall` 通过 |
| 发布包结构 | 干净解压，必需模块齐全，`import main` + `routes=12` 成功 |
| 发布包扫描 | 禁名 0、绝对路径 0、凭据 0 |
| schema / README 一致性 | 53 键全部有文档，无过期表述 |
| WebUI | JS 语法通过，87 个元素引用均有对应 ID |
| live 部署 | 0.12.1 已加载，页面 API 12、LLM 工具 1、状态正常 |
| live 与源码/发布目录 | 12 个关键文件 SHA256 完全一致 |
| 评论复核 live 冒烟 | 预览生成成功，未发公开评论 |

发布包：`release/astrbot_plugin_bili_scroll-0.12.1.zip`  
大小：104,324 B  
SHA256：`d47934d88e7bdb7e7f1173546c10ab32965e1281f37374b9c9965fa7fe55de9d`

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
11. **测试时间依赖**：夜间运行会被 quiet 时段误判；已固定测试配置。

## 发布前仍需处理

| 优先级 | 事项 | 状态 |
|---|---|---|
| P0 | GitHub 仓库不存在 | API 查询 `SIiOC` 当前只有 3 个公开仓库，没有 `astrbot-plugin-bili-scroll`；metadata 指向的地址 404，需先创建仓库 |
| P0 | 本地 git 发布候选 | 已初始化并提交/tag `v0.12.1`；remote 已指向 metadata 地址；因仓库不存在未推送 |
| P1 | 没有 README 截图/市场展示图 | 未处理 |
| P1 | 未提交 AstrBot 插件市场 | 未处理 |
| P1 | `comment_blocklist` 仍为空 | 需填入真实姓名/别名/单位等禁词 |
| P2 | `bilibili-api-python` 为 GPLv3+ 运行时依赖 | 已在 NOTICE 披露，建议发布前做许可确认 |
| P2 | 旧 `0.12.0` zip 缺少 `vision.py` | 不要分发；只用 0.12.1 |

## 个人信息扫描

- **0.12.1 发布包**：扫描全部文本条目，未发现主人 B 站昵称、QQ/WebChat 标识、手机号、身份证、邮箱、Windows 本地路径或实际凭据值；包里的 `sessdata`/`bili_jct` 只是字段名。
- **源码**：测试夹具里原有的 `Ivy`（WebChat 用户名）已替换为 `guest`；测试中的 `13800138000`、`a@b.com`、`1790000000` 是合成占位数据，不是真实个人信息。
- **live 运行数据**：`bili_credential.json`、`state.json`（B站昵称/QQ 会话/最近挑中）、`sent.jsonl`（评论正文与 @昵称）、`out/` 日报确实含个人信息，但位于 AstrBot `data/plugin_data/astrbot_plugin_bili_scroll/`，**不在发布包与源码中**。不要公开备份整个 `plugin_data` 目录或含这些字段的控制台截图。

## 建议发布顺序

1. `git init` → 提交 0.12.1 → 打 tag `v0.12.1`
2. 确认/创建 GitHub 仓库并推送，确认 CI 绿
3. 补页面截图与市场文案
4. 提交 AstrBot 市场
5. 在页面填入 `comment_blocklist`
