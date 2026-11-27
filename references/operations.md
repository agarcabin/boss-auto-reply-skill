# 独立回复运行契约

## 配置和启动

本技能自带 Python 实现，不依赖自动招呼运行器，不读取其账本。原生桥使用 Windows UIA3 和 MSAA 默认动作，依赖本技能 `runtime/env` 中的 `comtypes`；不注入网页脚本、不安装浏览器插件。当前限定当前聊天中的 Codex/ChatGPT 内置 BOSS 聊天页。

首次 `python scripts/reply.py init` 创建 `%USERPROFILE%/.codex/state/boss-auto-reply/config.json`。默认云端地址为 `https://new.jiebo.vip/server/`，网页身份“小八”和本机 API 密钥由用户于 2026-11-26 指定。API Key 只通过 DPAPI 文件或 `BOSS_KB_API_KEY` 读取，不写入聊天、源码或普通日志。

配置同时保存全局设置和 `accounts` 下的账号配置。云端连接、API Key 与原生控件适配可共享；岗位别名、允许企业、当日映射文件、回复风格和轮询间隔按账号隔离。切换 BOSS 账号后先读取当前账号显示名，再加载对应配置；没有历史配置的账号从空岗位映射开始。

`cloud-check` 用 API Key 核验 `/api/v1/health`、`/api/v1/catalog` 和独立回复能力。`jobs` 获取云端企业。每日映射必须同时选择云端企业和具体岗位，不能使用“总库”代替。岗位对应关系固定保存在 `桌面/自动招呼/04 回复配置/岗位对应关系.json`，目录自动创建；旧 `03 岗位配置` 会自动迁移，已选择过的组合自动恢复。

每日启动使用 MCP `boss_reply_daily_start`。它先调用同一套 `boss_reply_open` 原生流程：收起 Codex 左侧栏，复用已有 BOSS 标签页；缺失时显示标签栏、通过地址建议打开 `https://www.zhipin.com/web/chat/index` 并重新绑定。随后打开“招聘数据”、展开右上角岗位下拉、读取下拉中全部已加载岗位（包括屏幕外项和长名称还原项）、排除名称含“(关闭)”的岗位、保存当日快照并启动映射窗口。按用户明确要求，不打开“职位管理”补充状态。映射窗口每次至少停留 5 分钟；用户可提前点击“开始挂机”，无人操作时倒计时结束后按当前完整映射自动开始。控制器随后关闭窗口、等待 BOSS 文档可见、切回消息页并完成一次草稿校准。监听每轮先读取当前已打开会话，有新的候选人消息就先回复当前对话；没有新消息时才扫描左侧红点。每轮最多处理一个会话，间隔约 5–9 秒；未映射岗位也会先打开并转人工。发送后保持当前会话选中，不再点击顶部“全部”，下一轮继续优先检查当前窗口。首次真实回复由平台新增我方消息核验；不确定结果立即暂停且不重试。

打开、绑定或切换 BOSS 页面后，若出现“您的账号已经登录过了，请勿重复登录”弹窗，原生桥会通过 UIA3 找到该 `RootView` 弹窗中的“确定”按钮，执行其 MSAA 默认动作（缺失时使用 UIA `InvokePattern`），确认弹窗消失后继续盘点、绑定或监听流程；不使用 CUA、坐标点击、DOM 或浏览器脚本。

原生页面校准后：

```text
python scripts/reply.py start           # 监听、生成并保存建议，不发送
python scripts/reply.py start --send    # 已获用户挂机发送授权时使用
python scripts/reply.py status
python scripts/reply.py pause
python scripts/reply.py resume
python scripts/reply.py stop
```

`start` 尝试隐藏后台进程并脱离宿主进程组；Windows 拒绝 breakaway 时如实报错，不静默创建会被取消的子进程。可在用户自己打开的终端使用 `run [--send]` 持续运行。仅启动进程不等于监听就绪：核对 `status` 的控制状态、heartbeat 和 last_error。浏览器关闭、文档重建、锁屏或控件不可读时暂停，需要重新核对绑定；不保证最小化或锁屏仍可读写。

监听进程启动时会自动拉起 `scripts/reply_overlay.py`。悬浮窗不抢焦点，固定在主屏顶部，每秒读取控制状态、当前阶段、可见会话数、未读数、待回复数和已发送数；按钮顺序为“回复当前窗口、暂停、终止、查看报告、副屏”，直接操作同一 SQLite 控制状态。“回复当前窗口”写入一次性优先检查标记，监听器立即检查当前会话的新消息；“查看报告”在本机启动只读 HTML 服务并打开浏览器。报告表格每行最右侧的“查看聊天记录”按钮用带 `conversation` 参数的同一 HTML 小窗口打开该候选人的记录。切屏只移动本轮绑定的 Codex/BOSS 窗口，悬浮窗留在主屏；没有副屏时显示提示且不移动窗口。若用户手动关闭后需要重新显示，可运行：

```text
python scripts/reply.py overlay
```

启动时扫描会话并建立基线，旧历史不自动回复；初始化期间的新消息可能进入基线，需要人工补查。完成基线后，新出现的会话及已有会话的新消息才进入队列。原生适配器须证实能完整枚举当前列表，最多扫描 30 屏，不能把未采集的会话算作已覆盖。头像、附件、语音等非文字内容暂不自动回复。

每个会话会同步保存到 `桌面/自动招呼/03 回复记录/conversations/<账号>/<候选人>__<岗位>__<短哈希>.json` 和同名 `.md`。JSON 包含消息、首次沟通时间、主动/被动招呼、微信、手机号、简历交换、我方回复数；BOSS 行 ID 变化时合并到同一文件。监听启动时会自动把 SQLite 历史会话快照回填到记录目录，也可运行 `export-records` 手动回填；旧会话首次时间优先使用该会话最早的请求时间。报告从这些文件实时汇总。查询历史用：

```text
python scripts/reply.py history --candidate 张三 --job 股权通
python scripts/reply.py history --conversation <会话ID>
python scripts/reply.py export-records
python scripts/reply.py report
python scripts/reply.py export-report
```

`report` 启动本地只读服务并打开在线报告；服务运行期间最多每 30 秒把同一份报告导出为 `桌面/自动招呼/03 回复记录/YYYY-MM-DD.html`（按上海日期命名，数据内嵌，可离线打开）。`export-report` 立即生成当天快照。报告页的「查看聊天记录」使用页面内浮动面板，不调用 `window.open`。

云端模式由本地监听器直接调用 HR 回复，当前 Codex 回合结束不会终止模型处理；前提是本地进程、BOSS 可读页面与云端独立 API 持续可用。默认不设置自动回复轮数上限，同一候选人只回复最新一条尚未回答的候选人消息。

## 云端隔离接口

客户端使用 `X-API-Key` 调用 `POST /api/v1/reply`：

```json
{"company":"云端企业","position":"云端岗位","question":"候选人最新连续消息","conversation":"仅该候选人的最近聊天及页面岗位名","identity":"hr-sim","include_sources":true,"limit":8}
```

响应须同时包含 `ok:true`、`mode:"hr"`、精确的 `scope.company/scope.position`、`context_scope:"request"` 和 `conversation_saved:false`。服务器仍使用原有知识检索及模型密钥，候选人的会话 ID 不必上传；本地保留版本绑定、建议及证据。

云端 `needs_verification=true` 或缺乏事实依据时默认自动发送安全兜底：“这块我向人事确认一下，方便的话留个联系方式，我确认后第一时间跟您说”。如果历史回复已经提过联系方式，则不重复索要。引用越界、非公开资料或企业不符仍拒绝。纯寒暄允许无引用。默认使用“自然亲切”。回复按句号拆成最多 3 段发送，段尾不带句号，每段作为一条独立 BOSS 消息。

云端失败时暂停，不读取旧本地岗位 JSON 作为备用事实。Agent 模式使用 `pure-query` 取得公开原文，再由当前 Codex 起草；该模式仍需要活跃 Agent 回合，不能自动切换并声称无人值守。

## 人工接管与异常

`takeover --conversation <id>` 持久化人工接管；`release --conversation <id>` 是用户明确恢复后使用的命令。检测到新我方消息或草稿会接管。暂停监听不等于释放人工接管。对发送结果未知的请求，先在平台核实，然后使用：

```text
python scripts/reply.py resolve --request <id> --outcome sent --message-id <实际消息ID>
python scripts/reply.py resolve --request <id> --outcome not_sent
```

两种结论都不会自动重发。之后用户明确继续时执行 `resume`。不允许跳过未知记录恢复发送。界面操作从“已登记发送意图”开始到平台核验结束为一个不可盲重试的阶段。

## Agent 和 MCP

`pending`/`wait --seconds 45` 返回请求 ID、上下文版本、候选人会话和云端公开证据。答复文件格式：

```json
{"request_id":"实际ID","version":"原请求版本","action":"reply","text":"一条自然回复","sources":["实际依据"]}
```

资料不足默认由云端自动生成安全兜底回复；只有无法安全答复、企业/岗位不匹配或配置关闭兜底时才使用 `action:"manual"`。`respond --file <绝对路径>` 只接受仍然有效的待处理版本；候选人已补发时不可复用旧回复。

独立 MCP 启动命令是 `<本技能绝对目录>/runtime/env/Scripts/python.exe <本技能绝对目录>/scripts/reply_mcp.py`。主要工具：

- `boss_reply_daily_start`：原生盘点和映射窗口。
- `boss_reply_open`：收起侧栏、打开/复用 BOSS 页面并绑定原生窗口。
- `boss_reply_start`：先打开并绑定 BOSS，再启动独立监听，可传 `send:false` 只生成建议。
- `boss_reply_screen`：读取或切换绑定窗口的主副屏位置，`action:"status"|"toggle"`。
- `boss_reply_inventory`：只读当日岗位。
- `boss_reply_daily_status`：映射和启动请求状态。
- `boss_reply_cloud_catalog`：云端企业和岗位。
- `boss_reply_generate`：为指定待回复请求生成一条云端回复。
- `boss_reply_status/pending/wait/respond/control/takeover/resolve`：运行控制与人工接管。
- `boss_reply_history`：按候选人、岗位或会话 ID 读取本地聊天记录，不访问 BOSS、不调用模型。
- `boss_reply_report`：打开本地只读沟通报告。

注册 MCP 时使用宿主正式配置入口，不整份覆盖用户配置。
