# astrbot_plugin_broadcast

面向 **QQ 官方机器人**的全群广播插件：管理员一条指令把消息发到所有群，支持避开名单、Cron 定时广播，并内置发送间隔、每分钟限速与每日额度保护。

## 功能

| 能力 | 说明 |
| --- | --- |
| 手动广播 | `/broadcast <消息内容>`，向所有已登记群发送 |
| 权限控制 | 仅 `admin_uids` 中的用户可用；未配置时回退到 AstrBot 全局管理员 |
| 避开名单 | `exclude_umos` 命中的群自动跳过，支持完整 UMO 或纯群号 |
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
| `/broadcast_add <UMO\|群号>` | 手动登记一个群 |
| `/broadcast_remove <UMO\|群号>` | 移除一个群 |
| `/broadcast_status` | 查看额度、限速、定时任务状态与下次触发时间 |
| `/broadcast_reload` | 按当前配置重建定时任务（改完 Cron 不想重载插件时用） |

## 关键前提：群列表从哪来

> QQ 官方开放平台**没有**“查询机器人已加入的全部群”的接口，只有 `/v2/groups/{group_openid}/info` 这类单群查询。因此本插件不能像 OneBot 那样直接拉全量群列表。

插件用三种方式维护群列表，并持久化到 `data/plugin_data/astrbot_plugin_broadcast/groups.json`：

1. **自动登记**：机器人在某个群收到任意一条消息，就把该群的 UMO 记下来（`auto_discover=true` 时生效）；
2. **协议端同步**：如果同时挂了 aiocqhttp（NapCat / Lagrange），会调用 `get_group_list` 补全群列表；
3. **手动登记**：`/broadcast_add <群号或完整 UMO>`。

所以**首次使用**时，先让机器人在每个目标群各收到一条消息（或在群里发一条指令），再执行广播。用 `/broadcast_list` 可以确认登记结果。

UMO 格式：`平台ID:GroupMessage:群ID`，例如 `qq_official:GroupMessage:1A2B3C...`。注意 UMO 第一段是**平台适配器实例 ID**（QQ 官方默认是 `qq_official`），不是你给机器人起的名字。

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

## 关于限速

QQ 官方接口的公开限制约为 **20 次/分钟**，主动消息额度约 **1000 条/天**（以官方后台为准）。默认 `send_interval=2` 时理论速率是 30 条/分钟，会被 `max_per_minute=20` 压回 20 条/分钟；群数量多、失败率偏高时，建议把 `send_interval` 调到 3 秒以上，让节奏更平滑。

额度计数只统计**广播本身**发出的消息，广播结束的回执、被动回复不计入，实际消耗会比显示值略高。

## 定时广播

- 使用 APScheduler 的 `CronTrigger.from_crontab`，与 AstrBot 自带调度器同源，不需要额外安装依赖。
- 只支持**单条**定时任务。需要多个时间点/多条内容时，复制本插件目录改个名，或在 AstrBot「未来任务」里配合使用。
- 修改 `schedule_cron` 后执行 `/broadcast_reload`，或直接重载插件。
- 定时任务没有回执会话，执行结果写日志（`[broadcast] 广播结束：...`）。

## 已知限制

- QQ 官方机器人只能在**收到过消息**的群里主动发言；插件会把已登记群的会话场景预热到适配器缓存里，缓解 AstrBot 重启后“有群但发不出去”的问题，但前提是该群确实与机器人有过交互。
- 群 ID 是 `group_openid`，同一机器人换环境后可能变化，换号/迁移后需要重新登记。
- 未做长文本分段：QQ 官方对单条消息长度有限制，超长内容可能被判失败。
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
