# astrbot_plugin_broadcast

AstrBot 全群广播插件：管理员一条指令把消息发到所有群，支持避开名单、Cron 定时广播，并内置发送间隔、每分钟限速与每日额度保护。

插件为 **QQ 官方机器人**设计（额度与限速默认值按它的限制设定），但广播逻辑本身不依赖 QQ：只要平台适配器实现了群会话与主动发送，群目标统一使用 AstrBot 的规范化 UMO `平台ID:GroupMessage:群号`。

## 功能

| 能力 | 说明 |
| --- | --- |
| 手动广播 | `/broadcast <消息内容>`，向所有已登记群发送 |
| 权限控制 | 仅 `admin_uids` 中的用户可用；未配置时回退到 AstrBot 全局管理员 |
| 避开名单 | `exclude_umos` 命中的群自动跳过，支持完整 UMO 或纯群号 |
| 群列表同步 | 挂载 aiocqhttp 时，可在插件配置里一键拉取 `get_group_list` 写入群表 |
| 定时广播 | 五段式 Cron（如 `0 9 * * *`），可配时区 |
| 防封控 | 每个群之间 `send_interval`（默认 2s）+ 滑动窗口 `max_per_minute`（默认 20）+ `daily_limit`（默认 1000） |
| 结果反馈 | 广播结束在触发指令的会话回执：成功数、失败数、失败群 UMO 与原因 |

## 安装

1. 把 `astrbot_plugin_broadcast` 整个目录放到 AstrBot 的 `data/plugins/` 下。
2. 打开 WebUI → 插件 → 重载插件（或重启 AstrBot）。
3. 进入插件配置，填写 `admin_uids`、`exclude_umos` 等配置项。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/broadcast <消息>` | 向所有已登记群广播（别名 `/广播`） |
| `/broadcast_list` | 查看已登记群（已按避开名单过滤） |
| `/broadcast_add <平台ID>:GroupMessage:<群号>` | 手动登记一个群 |
| `/broadcast_remove <平台ID>:GroupMessage:<群号>` | 移除一个群 |
| `/broadcast_status` | 查看额度、限速、定时任务、群列表同步可用性与下次触发时间 |
| `/broadcast_reload` | 按当前配置重建定时任务（改完 Cron 不想重载插件时用） |

登记一律使用规范化 UMO，不接受裸群号，也不做平台推断。例如：

```
/broadcast_add qq_official:GroupMessage:1A2B3C...
/broadcast_add telegram:GroupMessage:-1001234567890
```

`/broadcast_remove` 允许只写群号作为过滤条件，但同一群号在多个平台下都存在时会拒绝执行，要求写全 UMO。

## 群列表从哪来

群表持久化在 `data/plugin_data/astrbot_plugin_broadcast/groups.json`，有三个来源：

1. **自动登记**：机器人在某个群收到任意消息就记录该群（`auto_discover=true` 时生效）；
2. **配置页同步**：挂了 aiocqhttp（NapCat / Lagrange）时，打开配置项「立即同步群列表（仅 aiocqhttp 可用）」并保存，插件会调用 `get_group_list` 拉取全量群；
3. **手动登记**：`/broadcast_add`。

### 为什么同步要手动触发

多数平台（含 QQ 官方开放平台）没有“查询机器人已加入的全部群”的接口：QQ 官方只有 `/v2/groups/{group_openid}/info` 这类单群查询，Telegram Bot API 同样不提供群列表。只有 OneBot 协议端（aiocqhttp）有标准的 `get_group_list`。

所以插件不在每次广播前自动拉取，而是把它做成配置页里的一个开关式触发器：

- 配置页没有可点击按钮（AstrBot 的 `_conf_schema.json` 只支持字符串、数字、布尔等输入型控件），保存配置会热重载插件，因此用 bool 开关当触发器；
- 打开开关并保存 → 插件加载时立即执行一次同步，把结果写入群表，然后**自动把开关复位为关闭**；
- 未挂载 aiocqhttp 时打开无效，日志会提示并跳过，不影响其他平台。

同步只负责“补全已加入的群”，不会删除群表里已有的记录；退群后仍残留的条目请用 `/broadcast_remove` 清掉。

## 配置项

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `admin_uids` | list | `[]` | 管理员 UID。**QQ 官方群聊里这是 member_openid，不是 QQ 号**；发一次 `/broadcast`，权限不足的提示里会带出你的 UID |
| `exclude_umos` | list | `[]` | 避开名单，写完整 UMO 或纯群号都可以 |
| `send_interval` | float | `2.0` | 每个群之间的间隔（秒） |
| `enable_schedule` | bool | `false` | 是否启用定时广播 |
| `schedule_cron` | string | `0 9 * * *` | 五段式 Cron：分 时 日 月 周 |
| `schedule_message` | text | `""` | 定时广播内容，留空则到点不发 |
| `timezone` | string | `Asia/Shanghai` | Cron 解释时区、每日额度重置时区 |
| `daily_limit` | int | `1000` | 每日主动消息上限，`0` 为不限 |
| `max_per_minute` | int | `20` | 滑动窗口限速，`0` 为不限 |
| `send_timeout` | int | `20` | 单条发送超时（秒） |
| `auto_discover` | bool | `true` | 是否自动登记收到消息的群 |
| `sync_groups_now` | bool | `false` | 开关式触发器：打开并保存后立即同步一次 aiocqhttp 群列表，完成后自动关闭 |

## 关于限速

默认值是照 QQ 官方接口设的：公开限制约为 **20 次/分钟**，主动消息额度约 **1000 条/天**（以官方后台为准）。默认 `send_interval=2` 时理论速率是 30 条/分钟，会被 `max_per_minute=20` 压回 20 条/分钟；群数量多、失败率偏高时，建议把 `send_interval` 调到 3 秒以上。

换到限制更宽松的平台（如 Telegram）时，把 `send_interval` 和 `max_per_minute` 调大即可；`daily_limit` 填 `0` 表示不限额。

额度计数只统计**广播本身**发出的消息，广播结束的回执、被动回复不计入，实际消耗会比显示值略高。

## 定时广播

- 使用 APScheduler 的 `CronTrigger.from_crontab`，与 AstrBot 自带调度器同源，不需要额外安装依赖。
- 只支持**单条**定时任务。需要多个时间点/多条内容时，复制本插件目录改个名，或在 AstrBot「未来任务」里配合使用。
- 修改 `schedule_cron` 后执行 `/broadcast_reload`，或直接重载插件。
- 定时任务没有回执会话，执行结果写日志（`[broadcast] 广播结束：...`）。

## 已知限制

- **QQ 官方**：机器人只能在收到过消息的群里主动发言；插件会把已登记群的会话场景预热到适配器缓存里，缓解 AstrBot 重启后“有群但发不出去”的问题，但前提是该群确实与机器人有过交互。
- **QQ 官方**：群 ID 是 `group_openid`，换号/迁移后可能变化，需要重新登记。
- 其他平台能否主动群发取决于适配器实现；若某平台不支持无消息上下文的发送，广播会在该群失败并在回执里给出原因。
- 未做长文本分段：平台对单条消息长度有限制，超长内容可能被判失败。
- 插件不做消息内容审核，请自行确保广播内容合规。

## 文件结构

```
astrbot_plugin_broadcast/
├── metadata.yaml       # 插件元数据
├── _conf_schema.json   # WebUI 配置结构
├── main.py             # 核心逻辑
├── requirements.txt    # 依赖（apscheduler 已随 AstrBot 安装）
└── README.md
```
