# BOSS 独立挂机回复

当前版本：`0.0.2-beta`

本技能在 Windows 上独立监听 BOSS 候选人消息，按岗位映射调用云端知识库生成回复，并通过原生 UIA/MSAA 桥完成输入、发送和回列表。它不启动自动招呼。

## 主要能力

- 收起 Codex 侧栏，复用或重新打开 BOSS 页面并绑定窗口。
- 读取左侧未读红点和当前已打开会话。
- 按岗位映射调用独立云端 HR 回复接口。
- 资料不足时自动发送“向人事确认 + 留联系方式”的安全兜底。
- 按句号拆成最多 3 段发送，段尾不带句号。
- 悬浮窗提供暂停、停止和主副屏切换。
- MCP 提供打开 BOSS、启动监听、状态、待回复、生成、控制和切屏接口。

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
