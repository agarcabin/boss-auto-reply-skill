# BOSS 独立挂机回复

当前版本：`0.0.3`

本技能在 Windows 上独立监听 BOSS 候选人消息，按岗位映射调用云端知识库生成回复，并通过原生 UIA/MSAA 桥完成输入、发送和回列表。它不启动自动招呼。

## 主要能力

- 收起 Codex 侧栏，复用或重新打开 BOSS 页面并绑定窗口。
- 每轮先读取当前已打开会话，有新消息优先回复；没有新消息再读取左侧未读红点。
- 按岗位映射调用独立云端 HR 回复接口。
- 资料不足时自动发送“向人事确认 + 留联系方式”的安全兜底。
- 按句号拆成最多 3 段发送，段尾不带句号。
- 悬浮窗按钮顺序：回复当前窗口、暂停、终止、查看报告、副屏。
- 每个会话自动落盘到 `Desktop/自动招呼/03 回复记录`，记录微信、手机号、简历交换和我方回复数。
- 岗位映射勾选状态写入配置 `job_enabled` / `enabled_jobs` / `disabled_jobs`；未勾选岗位不进入自动回复白名单。
- 无法自动回答的转人工问题会写入记录并在报告中标红“未完全回复”，聊天末尾列明问题、原因和时间。
- 启动时自动回填 SQLite 中已有的历史会话，也可运行 `reply.py export-records` 手动回填。
- “查看报告”打开本地只读 HTML 报告，支持实时刷新、搜索、筛选和按时间排序。
- 报告表格每行最右侧有“查看聊天记录”按钮，用 `?mode=history&conversation=<ID>` 打开该候选人的同一个报告 HTML 小窗口。
- 每日岗位映射窗口倒计时 5 分钟，超时后按完整映射自动开始。
- MCP 提供打开 BOSS、启动监听、状态、待回复、生成、控制和切屏接口。
- MCP 提供 `boss_reply_history` 本地聊天历史查询和 `boss_reply_report` 报告入口。

## 安装

1. 将本目录放到 `%USERPROFILE%\.codex\skills\boss-auto-reply`。
2. 创建原生运行环境：

```powershell
python -m venv runtime\env
.\runtime\env\Scripts\python.exe -m pip install -r requirements.txt
```

3. 在 Codex 的 `config.toml` 中注册 MCP：

```toml
[mcp_servers.boss-auto-reply]
command = 'C:\Users\<user>\.codex\skills\boss-auto-reply\runtime\env\Scripts\python.exe'
args = ['C:\Users\<user>\.codex\skills\boss-auto-reply\scripts\reply_mcp.py']
startup_timeout_sec = 30.0

[mcp_servers.boss-auto-reply.env]
BOSS_REPLY_STATE_DIR = 'C:\Users\<user>\.codex\state\boss-auto-reply'
```

## 首次配置

```powershell
python scripts\reply.py init
python scripts\reply.py doctor
python scripts\reply.py cloud-check
```

配置、API Key 封装、SQLite 队列和日志保存在 `%USERPROFILE%\.codex\state\boss-auto-reply`，不会提交到仓库。

## 测试

```powershell
.\runtime\env\Scripts\python.exe -m unittest discover -s tests -v
```

## 安全边界

- 不把候选人消息、岗位资料或模型返回当作控制指令。
- 不编造岗位事实；资料不足时使用安全确认话术。
- 企业或岗位不匹配、上下文隔离失败、发送结果未知时暂停。
- 人工草稿或人工新消息会接管当前会话，自动回复不覆盖人工输入。
