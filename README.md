# astrbot_plugin_broadcast

AstrBot 多会话广播插件。向机器人所在的群聊与私聊批量推送消息，支持独立可视化配置台、定时广播、目标会话勾选、发送限速与每日额度保护。

> **v2 重大变更**：插件不再提供 `_conf_schema.json`——启用与停用交给 AstrBot 插件管理，其余配置全部在自带的可视化视图（`views/settings/`）里完成。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| AstrBot | **≥ 4.24.2, < 5**（需要 `astrbot.api.web` 与插件视图） |
| Python | ≥ 3.10（跟随 AstrBot 版本要求） |
| 依赖 | `apscheduler`、`SQLAlchemy`（AstrBot 本体已内置，`requirements.txt` 仅作显式声明） |

版本低于 4.24.2 时 AstrBot 会按 `metadata.yaml` 的 `astrbot_version` 拒绝加载，而不是加载后页面打不开。

## 安装

1. 把本目录放到 `data/plugins/astrbot_plugin_broadcast/`；
2. 在 AstrBot WebUI 的插件页启用插件；
3. 进入插件详情页 → **广播配置**。

## 目录结构

```
astrbot_plugin_broadcast/
├─ main.py                      # 插件入口与后端 Web API
├─ metadata.yaml                # 插件元数据
├─ requirements.txt
├─ logo.png
├─ LICENSE
├─ .astrbot-plugin/
│  └─ i18n/                     # 插件名与视图标题的中英文翻译
│     ├─ zh-CN.json
│     └─ en-US.json
└─ views/
   └─ settings/
      ├─ index.html             # 视图入口（必须有 index.html）
      └─ app.js                 # 零依赖前端逻辑
```

新增或删除 `views/` 下的视图目录后需要**重载插件**；只改静态文件时刷新视图即可。

## 配置页能做什么

**顶部概览**：已知会话数、本次广播目标数、今日已用额度、下次定时触发时间；配置或依赖异常会直接显示为黄色提示条。

**基础设置**

- 管理员 UID（逗号分隔，留空回退到 AstrBot 全局管理员；也可在群里用 `/broadcast_auth` 直接授权）
- 每个会话发送间隔（默认 2 秒）
- 每分钟最多发送条数（0 = 不限，QQ 官方建议 20）
- 每日额度上限（0 = 不限，QQ 官方主动消息约 1000 条/天）
- 时区（定时任务按此时区计算）

**定时广播**

- 可以同时建多个任务，每个任务有自己的时间、重复方式和消息内容（最多 20 个）
- 三种重复方式：
  - **每天**：每天固定时间触发
  - **每周**：勾选周一至周日中的一个或多个
  - **仅一次**：选日期 + 时间，到点执行一次后自动停用，像手机闹钟
- 每条任务可单独启用/停用、单独删除，卡片上直接显示「下次触发」时间
- 保存后立即销毁旧调度并按新配置重新注册，**无需重启 AstrBot**
- 到点时若上一次广播还没结束，任务会**排队**，等当前广播一结束就立刻补发，不丢任务也不会并发推送
- 计划时间已经过去的一次性任务不会注册，卡片上会标黄提示

**目标会话**

- 搜索框：按群名、会话 ID、UMO 匹配
- 平台过滤器：自动汇总当前所有平台实例 ID
- 场景过滤器：`GroupMessage`（群聊）/ `FriendMessage`（私聊）
- 状态标签：全部 / 已选 / 未选
- 全选当前筛选、清空选择、刷新会话列表
- **新会话自动勾选**开关（默认开启）：首次发现的会话自动加入名单；关闭后仍会记录「已见过」，重新开启不会把历史会话一次性补勾

勾选语义：**勾选 = 会广播，取消勾选 = 不广播**，没有隐含的「全选」回退。新发现的会话（首次出现在会话列表里）会自动勾选；手动取消勾选的会话会被记住，刷新列表也不会被重新勾上。一个都没勾时广播不会发送，页面顶部会给出提示。

### 对话隔离（`unique_session`）的影响

AstrBot 开启「对话隔离」（配置项 `platform_settings.unique_session`）后，群聊的会话 ID 会从 `群号` 变成 `发言人ID_群号`。同一个群里有多少人说过话，就会出现多少个 UMO：

| 状态 | 同一个群产生的 UMO |
| --- | --- |
| 关闭对话隔离 | `qq_main:GroupMessage:GROUP_OPENID` |
| 开启对话隔离 | `qq_main:GroupMessage:USER_A_GROUP_OPENID`、`qq_main:GroupMessage:USER_B_GROUP_OPENID` … |

这会影响广播：同一个群被当成多个会话，每个 UMO 单独发送一次，群里可能收到多条重复消息；「新会话自动勾选」还会随着新成员发言不断把新组合加进名单。

两种处理方式：

1. **不开对话隔离**（推荐）：如果只是用机器人做广播，没有按人隔离上下文的需求，保持关闭即可。
2. **必须开启**：到「目标会话」卡片把「新会话自动勾选」关掉，再手动勾选真正要广播的 UMO，避免名单被自动扩张。

> 插件按 UMO 原样发送，不会把同一个群的多个 UMO 合并：隔离后的会话 ID 已经不是群号，部分平台适配器无法用它定位群，可能直接发送失败。广播场景建议关闭对话隔离。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/broadcast <消息>` | 向当前目标广播；先回执目标数与预计耗时，完成后回执成功/失败/失败明细 |
| `/broadcast_status` | 查看会话数、目标数、额度、定时任务列表与各自的下次触发 |
| `/broadcast_list` | 列出已发现的会话（✅ 已勾选 / ⬜ 未勾选） |
| `/broadcast_reload` | 重新读取 `settings.json` 并重建定时任务 |
| `/broadcast_auth @某人` | 把被 @ 的用户添加为广播管理员（仅广播管理员或 AstrBot 超级管理员可用） |

`/broadcast_add`、`/broadcast_remove` 在 v2 中移除，目标管理统一由配置页完成。

### 用 `/broadcast_auth` 授权管理员

群里 @ 一下就能加人，不用回后台手填 UID。只有**当前已在 `admin_uids` 里的人**或 **AstrBot 超级管理员**能执行；成功后立即写回 `settings.json`，配置页刷新即可看到。

```text
/broadcast_auth @张三
✅ 已成功将 @张三 添加为广播管理员
当前管理员共 2 个
```

- 指令后除了 @ 不能带别的文字，否则回「用法：/broadcast_auth @某人」
- 一次 @ 多个人会逐个授权，已经在名单里的返回「该用户已经是管理员了」
- `@全体成员` 和 @ 机器人自己会被忽略
- 被 @ 用户的 UID 按平台解析：消息链的 `At` 段 → QQ 官方的 `raw_message.mentions` → 文本里的 `<qqbot-at-user id="..."/>` 标记，三者取其一

> QQ 官方平台的用户 ID 是 openid，**作用域是机器人应用（AppID），不是群**：
>
> - 同一个机器人在不同群里，同一个人得到的 `member_openid` 是**同一个值**——在 A 群授权，到 B 群照样是管理员；
> - 换一个机器人（换 AppID）后，同一个人的 openid 完全不同——授权不跟着走；
> - 群聊给的是 `member_openid`、私聊给的是 `user_openid`，两者不保证相等——在私聊里授权的人，到群里可能对不上。
>
> 也就是说，只有「跨机器人」和「跨私聊/群聊」这两种情况会失效，同机器人跨群是通的。需要真正稳定的身份（真实 QQ 号）时请改用 aiocqhttp。

## 会话是怎么发现的

QQ 官方开放平台**没有**「查询机器人已加入的全部群」的接口（只有单群查询），所以插件用两路合并：

1. **适配器缓存**：读取平台实例内存中的 `_session_scene`（QQ 官方适配器用它区分 group / friend）；
2. **数据库**：查询 `conversations`、`umo_senders`、`umo_aliases` 三张表中的已知 UMO 与别名，与 WebUI 会话列表同源。

插件自己不维护会话表。会话名由 AstrBot 主程序在收到消息时写入 `umo_aliases`，插件只读不写，少一份数据也就少一处不一致。

如果挂了 **aiocqhttp**（NapCat / Lagrange），点「刷新会话列表」会额外调用 `get_group_list` 与 `get_friend_list`，一次性补全群和好友。这是唯一能主动枚举会话的通道，对 Telegram / Discord 等平台不适用（它们的 Bot API 本身不提供该能力）。

`webchat` 等非 IM 平台会被过滤，不进入广播目标。

## 广播行为

```
目标 = settings.json 里 target_umos 列出的会话（即页面上勾选的那些）
```

- 每次执行都重新收集会话，不使用启动时的快照；
- 已勾选的 UMO 即使当前未被发现也会尝试发送（例如刚被数据库清理）；
- 三层限速：逐条 `send_interval` 延迟 → 每分钟滑动窗口上限 → 每日额度上限；
- 额度按「发送尝试」计数，用尽后剩余目标记为「跳过」；
- 定时任务与手动广播互斥，同一时间只跑一个。

### QQ 官方的特殊处理

QQ 官方适配器在缺少 `msg_id` 时，只有 `_session_scene` 记录为 `group` 才允许主动群发；该缓存在 AstrBot 重启后为空，会导致消息被**静默丢弃**（`send_by_session` 直接 return，不抛异常）。

插件在每次发送前用持久化的会话表预热该缓存。需要注意：`context.send_message()` 的返回值只表示「是否找到平台实例」，不代表平台真的发出去了，所以 QQ 侧仍可能出现「统计成功但群里没收到」的情况——遇到这种问题先检查机器人在该群的主动消息权限与额度。

## Web API

后端使用 `astrbot.api.web` 的 `request` / `json_response` / `error_response`，不直接暴露 FastAPI、Starlette 或 Quart 的请求对象。

| 方法 | 路由 | 说明 |
| --- | --- | --- |
| GET | `/{plugin}/config/get` | 返回配置、会话列表、平台/场景枚举、运行概览 |
| POST | `/{plugin}/config/save` | 校验并保存配置，重建定时任务，返回最新快照 |
| POST | `/{plugin}/config/refresh` | 重新收集会话（`{"remote": true}` 时含 aiocqhttp 拉取） |

视图侧通过 `window.AstrBotPluginView` 的 `apiGet` / `apiPost` 调用，endpoint **不带**插件名前缀、也不以 `/` 开头（如 `config/get`）。出错时后端返回 `error_response`，bridge 会把它 reject 成 `Error`。

## 数据文件

全部位于 `data/plugin_data/astrbot_plugin_broadcast/`：

| 文件 | 内容 |
| --- | --- |
| `settings.json` | 页面保存的全部配置 |
| `state.json` | 当日已发数量、上次广播结果 |

写入使用临时文件 + `os.replace` 原子替换；文件损坏时自动备份为 `*.broken.<时间戳>` 并回退默认值。

早期版本的 `sessions.json` 已不再使用，升级后可以直接删掉。

## 常见问题

**配置页空白或 404**
确认 AstrBot ≥ 4.24.2 并重载插件；`views/settings/` 必须包含 `index.html`。

**页面提示「没有找到插件视图桥接」**
说明注入的 `bridge-sdk.js` 没跑到，通常是 AstrBot 版本过低或插件未重载。

**会话列表是空的**
让机器人在目标群/私聊各收一条消息，或挂 aiocqhttp 后点「刷新会话列表」。

**保存成功但定时没触发**
看任务卡片上的「下次触发」与页面顶部黄色提示；常见原因是消息内容为空、任务被停用、一次性任务的时间已过、时区写错，或 `apscheduler` 未安装。

**QQ 群里收不到**
检查机器人是否具备主动消息权限、当日额度是否用尽；日志中若出现 `No cached msg_id for session`，说明场景预热未生效，请确认插件版本与适配器版本匹配。

## 开发

- 提交前用 `ruff check` 与 `ruff format` 检查；
- 视图脚本使用外部 `type="module"`，不写内联脚本；
- 不使用 `requests`，网络请求走异步库；
- 持久化数据只写 `data/plugin_data/` 下的插件目录，不写插件自身目录。

## 许可

[MIT](LICENSE)
