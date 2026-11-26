# 原生 BOSS 页面校准

当前原生桥使用 UIA3 读取和 MSAA 默认动作；没有把 Edge 插件的 DOM 读取结果当成原生测试结果。默认配置的控件类名为空，发送关闭。

1. 在当前 Codex 聊天的内置浏览器打开 BOSS 页面。确认当前聊天标题及所属窗口句柄。MCP `boss_reply_daily_start` 在原生桥可见后自动绑定；也可用 `reply.py bind --window <HWND> --chat-title <精确聊天标题>`。绑定校验宿主进程、创建时间、当前聊天文档和唯一 BOSS 文档。绑定变化会清除校准状态。
2. `probe` 只返回 BOSS 文档内相关控件的类名、控件类型及是否有 AutomationId，不读取整桌面或输出聊天正文。根据真实 UIA 层级填写 profile，不能凭 DOM 类名猜测通过。
3. `layout_mode=text_sequence` 时，profile 的 list_class/item_class 定位会话列表与条目；条目正文按顶层 Text 控件的顺序解析，未读数字是可缺省的第一项，日期之后依次为姓名、岗位、预览。选中项用会话头 name_class/job_class 与条目姓名/岗位反查，不依赖页面未暴露的 `selected` 类。旧布局可显式设置 `layout_mode=classic` 再提供 unread_class/selected_class/contact_name_class。
4. message_root_class/message_class/message_text_class 分别定位聊天区、消息条目和正文。消息方向只看 message-item 的直接子控件：`figure` 或 `item-friend` 为候选人，仅 `text` 为招聘方，`message-card-wrap` 为系统卡片。正文必须位于唯一的 `text` 容器内，状态词“已读/送达”不进入对话。editor_class 是消息编辑器，send_class 是唯一原生发送按钮。
5. 会话身份使用宿主绑定、候选人姓名和岗位组成的逻辑键；同一列表出现重名同岗位时停止，不能猜选。消息身份在当前原生文档内使用 runtime ID、姓名与正文；列表重排不能复用消息回执。核对后才设置 verified 和 stable_ids_verified。
6. 监听只验证当前可见的会话区，不滚动到底。只处理带未读数字且岗位已映射的会话；每轮最多处理一个，发送后点击顶部“全部”取消选中，使下一条新消息重新形成红点。
7. 编辑器不暴露 ValuePattern 时，先确认 BOSS 顶层窗口已经是前台，再定位承载 BOSS URL 的唯一 `Chrome_RenderWidgetHost*` 子窗口，把原生焦点交给它并调用编辑器的 UIA `SetFocus`。只有 UIA 焦点确实落在 editor_class 后才用 `SendInput` 的 `KEYEVENTF_UNICODE` 分段输入；每四个字符及标点后通过 TextPattern 核对。不得使用坐标点击、回车发送或从后台抢焦点。
8. “开始挂机”本身是用户对自动回复的明确授权。控制器在映射保存后自动核对一次 `draft_test` 写入和清空，设置 draft_verified；首次真实回复仍必须取得新增我方消息的平台回执，成功后设置 send_verified。平台结果不确定时记录 unknown、暂停，不自动重试，也不把未确认结果当成成功。

当前桥仅遍历当前可访问聊天列表，不支持自动切换“沟通中/新招呼”等多个业务分类。应按用户选定的分类界定覆盖范围。消息区不在最新位置或正文/方向不完整时暂停，不猜测。系统版本、网页布局、浏览器 AX 暴露方式变化都可能需要重校准。

未经校准的运行结果应称为“独立运行器已建立，等待原生适配”，不能称为“BOSS 全量实时挂机已实现”。
