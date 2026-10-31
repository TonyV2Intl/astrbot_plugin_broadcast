# astrbot_plugin_broadcast

AstrBot 多会话广播插件。向机器人所在的群聊与私聊批量推送消息，支持独立可视化配置台、定时广播、目标白名单、排除名单、发送限速与每日额度保护。

> **v2 重大变更**：配置不再堆在 AstrBot 的「插件配置」弹窗里，改为插件自带的独立页面（AstrBot Pages）。`_conf_schema.json` 只剩一个总开关。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| AstrBot | **≥ 4.24.2**（需要 `astrbot.api.web` 与插件 Pages） |
| Python | ≥ 3.12（跟随 AstrBot 版本要求） |
| 依赖 | `apscheduler`、`SQLAlchemy`（AstrBot 本体已内置） |

低于 4.24.2 时插件仍能加载、指令仍可用，但**可视化配置页无法打开**，启动日志会给出提示。

## 安装

1. 把本目录放到 `data/plugins/astrbot_plugin_broadcast/`；
2. 在 AstrBot WebUI 的插件页启用插件；
3. 打开插件详情页 → **广播配置**（页面标题来自 `.astrbot-plugin/i18n/zh-CN.json`）。

新增或删除 `pages/` 下的视图目录后需要重载插件；只改静态文件时刷新页面即可。

## 配置页能做什么

**顶部概览**：已知会话数、本次广播目标数、今日已用额度、下次定时触发时间；配置或依赖异常会直接显示为黄色提示条。

**基础设置**

- 管理员 UID（逗号分隔，留空回退到 AstrBot 全局管理员）
- 每个会话发送间隔（默认 2 秒）
- 每分钟最多发送条数（0 = 不限，QQ 官方建议 20）
- 每日额度上限（0 = 不限，QQ 官方主动消息约 1000 条/天）
- 时区（定时任务按此时区计算）
- 自动登记会话

**定时广播**

- 开关、重复方式（每天 / 每周）、可视化时间选择（`<input type="time">`）
- 每周模式可点选周一至周日
- 页面实时预览生成的 Cron 表达式与字数
- 保存后立即销毁旧任务并注册新任务，**无需重启 AstrBot**

**目标会话**

- 搜索框：按群名、会话 ID、UMO 匹配
- 平台过滤器：自动汇总当前所有平台实例 ID
- 场景过滤器：`GroupMessage`（群聊）/ `FriendMessage`（私聊）
- 状态标签：全部 / 已选 / 未选 / 已排除
- 全选当前筛选、清空选择、刷新会话列表
- 每行右侧的 🚫 可把该会话加入「永不发送」名单

勾选语义：**一个都不勾 = 向全部会话广播**；勾选后 = 只向选中会话广播。清空选择会回到「全部」模式，页面会提示。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/broadcast <消息>` | 向当前目标广播；先回执目标数与预计耗时，完成后回执成功/失败/失败明细 |
| `/broadcast_status` | 查看开关、会话数、目标数、额度、定时状态与下次触发 |
| `/broadcast_list` | 列出已发现的会话（✅ 已选 / ⬜ 未选 / 🚫 已排除） |
| `/broadcast_reload` | 重新读取 `settings.json` 并重建定时任务 |

`/broadcast_add`、`/broadcast_remove` 在 v2 中移除，目标管理统一由配置页完成。

## 会话是怎么发现的

QQ 官方开放平台**没有**“查询机器人已加入的全部群”的接口（只有单群查询），所以插件用三路合并：

1. **自动登记**：机器人收到任意消息时记录该会话的 UMO（写入 `sessions.json`）；
2. **适配器缓存**：读取平台实例内存中的 `_session_scene`（QQ 官方适配器用它区分 group / friend）；
3. **数据库**：查询 `conversations`、`umo_senders`、`umo_aliases` 三张表中的已知 UMO，与 WebUI 会话列表同源。

如果挂了 **aiocqhttp**（NapCat / Lagrange），点「刷新会话列表」会额外调用 `get_group_list` 与 `get_friend_list`，一次性补全群和好友。这是唯一能主动枚举会话的通道，对 Telegram / Discord 等平台不适用（它们的 Bot API 本身不提供该能力）。

`webchat` 等非 IM 平台会被过滤，不进入广播目标。

## 广播行为

```
目标 = 白名单（为空则全部会话） − 排除名单
```

- 每次执行都重新收集会话，不使用启动时的快照；
- 白名单中的 UMO 即使当前未被发现也会尝试发送（例如刚被数据库清理）；
- 三层限速：逐条 `send_interval` 延迟 → 每分钟滑动窗口上限 → 每日额度上限；
- 额度按“发送尝试”计数，用尽后剩余目标记为「跳过」；
- 定时任务与手动广播互斥，同一时间只跑一个。

### QQ 官方的特殊处理

QQ 官方适配器在缺少 `msg_id` 时，只有 `_session_scene` 记录为 `group` 才允许主动群发；该缓存在 AstrBot 重启后为空，会导致消息被**静默丢弃**（`send_by_session` 直接 return，不抛异常）。

插件在每次发送前用持久化的会话表预热该缓存。需要注意：`context.send_message()` 的返回值只表示“是否找到平台实例”，不代表平台真的发出去了，所以 QQ 侧仍可能出现“统计成功但群里没收到”的情况——遇到这种问题先检查机器人在该群的主动消息权限与额度。

## Web API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/astrbot_plugin_broadcast/config/get` | 返回配置、会话列表、平台/场景枚举、运行概览 |
| POST | `/astrbot_plugin_broadcast/config/save` | 校验并保存配置，重建定时任务，返回最新快照 |
| POST | `/astrbot_plugin_broadcast/config/refresh` | 重新收集会话（`{"remote": true}` 时含 aiocqhttp 拉取） |

页面通过 `window.AstrBotPluginPage` 的 `apiGet` / `apiPost` 调用，endpoint 不带插件名前缀（如 `config/get`）。

## 数据文件

全部位于 `data/plugin_data/astrbot_plugin_broadcast/`：

| 文件 | 内容 |
| --- | --- |
| `settings.json` | 页面保存的全部配置 |
| `sessions.json` | 自动登记的会话（UMO → 名称、最后活跃时间） |
| `state.json` | 当日已发数量、上次广播结果 |

写入使用临时文件 + `os.replace` 原子替换；文件损坏时自动备份为 `*.broken.<时间戳>` 并回退默认值。

## 从 1.x 升级

首次启动时若 `settings.json` 不存在，插件会读取旧版 `_conf_schema.json` 中的 `admin_uids`、`exclude_umos`、`send_interval`、`schedule_cron`、`schedule_message` 等字段并迁移过去，包括把 `30 7 * * 1,3` 这类 Cron 反解成「每周 周一、周三 07:30」。迁移只发生一次，之后以 `settings.json` 为准。

## 常见问题

**配置页空白或 404**
确认 AstrBot ≥ 4.24.2，并重载插件；`pages/settings/` 必须包含 `index.html`。

**会话列表是空的**
让机器人在目标群/私聊各收一条消息，或挂 aiocqhttp 后点「刷新会话列表」。

**保存成功但定时没触发**
看页面顶部黄色提示与 `/broadcast_status` 的「下次触发」；常见原因是定时消息为空、时区写错、`apscheduler` 未安装。

**QQ 群里收不到**
检查机器人是否具备主动消息权限、当日额度是否用尽；日志中若出现 `No cached msg_id for session`，说明场景预热未生效，请确认插件版本与适配器版本匹配。
