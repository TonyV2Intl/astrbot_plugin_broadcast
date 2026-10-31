"""AstrBot 全群广播插件。

命令：
    /broadcast <消息内容>                        向所有已登记群聊广播
    /broadcast_list                              查看已登记的群聊
    /broadcast_add <平台ID>:GroupMessage:<群号>   手动登记一个群
    /broadcast_remove <平台ID>:GroupMessage:<群号> 移除一个群
    /broadcast_status                            查看额度、限速与定时任务状态
    /broadcast_reload                            按当前配置重建定时任务

说明：
    群列表采用“动态登记 + 本地持久化”的方式维护，登记时统一使用 AstrBot 的
    规范化 UMO：<平台ID>:GroupMessage:<群号>，不按平台做任何推断。
      1. 机器人收到任意群消息时，自动记录该群的 UMO；
      2. 挂了 aiocqhttp（NapCat / Lagrange 等）时，可在插件配置里打开
         「立即同步群列表」并保存，插件会调用 get_group_list 拉取全量群；
      3. 也可以通过 /broadcast_add 手动登记。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

try:  # AstrBot 自带 apscheduler（telegram 适配器依赖它）
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError:  # pragma: no cover
    AsyncIOScheduler = None  # type: ignore[assignment]
    CronTrigger = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_broadcast"
SCHEDULE_JOB_ID = "broadcast_cron"
GROUP_MESSAGE_TYPE = "GroupMessage"

DEFAULT_INTERVAL = 2.0
DEFAULT_DAILY_LIMIT = 1000
DEFAULT_MAX_PER_MINUTE = 20
DEFAULT_TIMEOUT = 20
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_CRON = "0 9 * * *"
SYNC_WAIT_SECONDS = 15


# --------------------------------------------------------------------------- #
# 限速器
# --------------------------------------------------------------------------- #
class RateLimiter:
    """滑动窗口限速器，用于把发送速率压到 N 条/分钟以内。"""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max(0, int(max_per_minute))
        self._timestamps: deque[float] = deque()

    async def acquire(self) -> None:
        """在允许发送时立即返回，否则等到窗口空出位置。"""
        if self.max_per_minute <= 0:
            return
        while True:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= 60.0:
                self._timestamps.popleft()
            if len(self._timestamps) < self.max_per_minute:
                self._timestamps.append(now)
                return
            wait = 60.0 - (now - self._timestamps[0]) + 0.05
            await asyncio.sleep(max(wait, 0.05))


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class BroadcastResult:
    """一次广播的结果统计。"""

    total: int = 0
    success: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    quota_blocked: bool = False
    elapsed: float = 0.0


@dataclass
class GroupRecord:
    """一个广播目标群。"""

    umo: str
    platform_id: str = ""
    group_id: str = ""
    name: str = ""
    last_seen: float = 0.0

    @classmethod
    def from_umo(cls, umo: str, name: str = "") -> GroupRecord:
        parts = umo.split(":", 2)
        if len(parts) == 3:
            return cls(
                umo=umo,
                platform_id=parts[0],
                group_id=parts[2],
                name=name,
                last_seen=time.time(),
            )
        return cls(umo=umo, name=name, last_seen=time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "umo": self.umo,
            "platform_id": self.platform_id,
            "group_id": self.group_id,
            "name": self.name,
            "last_seen": self.last_seen,
        }


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
class OfficialBroadcastPlugin(Star):
    """全群广播插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config

        self._data_dir = self._resolve_data_dir()
        self._registry_path = self._data_dir / "groups.json"
        self._state_path = self._data_dir / "state.json"

        self._groups: dict[str, dict[str, Any]] = {}
        self._state: dict[str, Any] = {"date": "", "sent": 0}
        self._busy = False
        self._task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None
        self._scheduler: Any = None

        self._load_registry()
        self._load_state()

        # 插件热重载时 __init__ 里也要建好定时任务，不能只依赖 on_astrbot_loaded
        try:
            self._setup_scheduler()
        except Exception:
            logger.exception("[broadcast] 初始化定时任务失败")

        # 配置页的「立即同步群列表」是开关式触发器：保存配置会热重载插件，
        # 加载时发现开关为 true 就执行一次同步，并把开关复位。
        if self._bool_conf("sync_groups_now", False):
            try:
                self._sync_task = asyncio.create_task(self._consume_sync_trigger())
            except RuntimeError:
                logger.warning("[broadcast] 当前没有事件循环，跳过群列表同步触发")

    # ------------------------------------------------------------------ #
    # 初始化辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_data_dir() -> Path:
        """数据目录：优先 data/plugin_data/<plugin>，退化为插件目录下 data/。"""
        try:
            path = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            try:
                from astrbot.core.utils.astrbot_path import (
                    get_astrbot_plugin_data_path,
                )

                path = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
            except Exception:
                path = Path(__file__).resolve().parent / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        """原子写入，避免进程被杀导致 JSON 截断。"""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def _load_registry(self) -> None:
        if not self._registry_path.exists():
            return
        try:
            raw = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except Exception as exc:
            backup = self._registry_path.with_suffix(".json.bak")
            with suppress(Exception):
                os.replace(self._registry_path, backup)
            logger.warning(f"[broadcast] 群列表文件损坏，已备份到 {backup.name}：{exc}")
            return

        groups = raw.get("groups") if isinstance(raw, dict) else None
        if not isinstance(groups, dict):
            groups = raw if isinstance(raw, dict) else {}
        for umo, item in groups.items():
            if not isinstance(umo, str) or not umo:
                continue
            if not isinstance(item, dict):
                item = {}
            try:
                record = GroupRecord.from_umo(umo, name=str(item.get("name") or ""))
                record.last_seen = float(item.get("last_seen") or record.last_seen)
            except (TypeError, ValueError):
                # 单条记录损坏不应让整个插件加载失败
                continue
            self._groups[umo] = record.to_dict()
        logger.info(f"[broadcast] 已加载 {len(self._groups)} 个群聊记录")

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        try:
            sent = int(float(raw.get("sent") or 0))
        except (TypeError, ValueError):
            sent = 0
        self._state = {"date": str(raw.get("date") or ""), "sent": max(0, sent)}

    def _save_registry(self) -> None:
        try:
            self._write_json(
                self._registry_path, {"version": 1, "groups": self._groups}
            )
        except Exception as exc:
            logger.warning(f"[broadcast] 保存群列表失败：{exc}")

    def _persist_state(self) -> None:
        try:
            self._write_json(self._state_path, self._state)
        except Exception as exc:
            logger.warning(f"[broadcast] 保存状态失败：{exc}")

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #
    def _conf(self, key: str, default: Any = None) -> Any:
        cfg = self.config
        if cfg is None:
            return default
        try:
            value = cfg.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _bool_conf(self, key: str, default: bool) -> bool:
        value = self._conf(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _int_conf(self, key: str, default: int) -> int:
        try:
            return int(float(self._conf(key, default)))
        except (TypeError, ValueError):
            return default

    def _float_conf(self, key: str, default: float) -> float:
        try:
            return float(self._conf(key, default))
        except (TypeError, ValueError):
            return default

    def _str_list_conf(self, key: str) -> list[str]:
        value = self._conf(key, [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            return []
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                result.append(text)
        return result

    def _timezone(self) -> Any:
        name = str(self._conf("timezone", DEFAULT_TIMEZONE) or "").strip()
        if not name or ZoneInfo is None:
            return None
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning(f"[broadcast] 无效时区 {name}，改用系统本地时区")
            return None

    # ------------------------------------------------------------------ #
    # 权限
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "").strip()
        except Exception:
            return ""

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        uid = self._sender_id(event)
        if not uid:
            return False

        admins = self._str_list_conf("admin_uids")
        if admins:
            return uid in admins

        # 未配置 admin_uids 时回退到 AstrBot 全局管理员，避免插件一装上就没人能用
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            global_admins = self.context.get_config().get("admins_id", []) or []
            return uid in {str(item).strip() for item in global_admins}
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 群列表维护
    # ------------------------------------------------------------------ #
    def _upsert_group(self, umo: str, group_name: str = "") -> bool:
        """登记/更新一个群，返回是否有需要落盘的变化。"""
        if not umo or umo.count(":") < 2:
            return False
        now = time.time()
        record = self._groups.get(umo)
        if record is None:
            self._groups[umo] = GroupRecord.from_umo(umo, group_name).to_dict()
            logger.info(f"[broadcast] 新登记群聊：{umo}")
            return True
        record["last_seen"] = now
        if group_name and record.get("name") != group_name:
            record["name"] = group_name
            return True
        return False

    def _register_event_group(self, event: AstrMessageEvent) -> bool:
        if not self._bool_conf("auto_discover", True):
            return False
        try:
            umo = str(event.unified_msg_origin or "")
            group_id = str(event.get_group_id() or "")
        except Exception:
            return False
        if not umo or not group_id:
            return False
        return self._upsert_group(umo)

    def _get_platform(self, platform_id: str) -> Any:
        try:
            return self.context.get_platform_inst(platform_id)
        except Exception:
            return None

    def _ensure_group_scene(self, umo: str) -> None:
        """补齐 QQ 官方适配器的群会话场景缓存。

        适配器只有在 _session_scene[session_id] == "group" 时才允许不带 msg_id
        的主动群消息；AstrBot 刚重启、群还没发过言时该缓存是空的，会导致广播被
        静默跳过。这里用持久化的群列表把它补上。
        """
        try:
            platform_id, message_type, session_id = umo.split(":", 2)
        except ValueError:
            return
        if message_type != GROUP_MESSAGE_TYPE:
            return
        platform = self._get_platform(platform_id)
        scenes = getattr(platform, "_session_scene", None) if platform else None
        if isinstance(scenes, dict):
            scenes.setdefault(session_id, "group")

    async def _discover_from_platforms(self) -> None:
        """从运行中的平台实例补充群列表（只读取适配器内存中已见过的群会话）。"""
        if not self._bool_conf("auto_discover", True):
            return
        try:
            instances = self.context.platform_manager.get_insts()
        except Exception as exc:
            logger.debug(f"[broadcast] 获取平台实例失败：{exc}")
            return

        changed = False
        for instance in instances:
            try:
                meta = instance.meta()
                platform_id = str(getattr(meta, "id", "") or "")
            except Exception:
                continue

            # 适配器内存中见过的群会话（QQ 官方适配器会记录 scene）
            scenes = getattr(instance, "_session_scene", None)
            if isinstance(scenes, dict):
                for session_id, scene in list(scenes.items()):
                    if scene != "group":
                        continue
                    changed |= self._upsert_group(
                        f"{platform_id}:{GROUP_MESSAGE_TYPE}:{session_id}"
                    )

        if changed:
            self._save_registry()

    def _platform_instances(self) -> list[Any]:
        try:
            return list(self.context.platform_manager.get_insts())
        except Exception:
            return []

    def _aiocqhttp_instances(self) -> list[tuple[str, Any]]:
        """返回 (平台实例 ID, 实例) 的 aiocqhttp 列表。"""
        found: list[tuple[str, Any]] = []
        for instance in self._platform_instances():
            try:
                meta = instance.meta()
            except Exception:
                continue
            if str(getattr(meta, "name", "") or "") != "aiocqhttp":
                continue
            platform_id = str(getattr(meta, "id", "") or "")
            if platform_id:
                found.append((platform_id, instance))
        return found

    async def _consume_sync_trigger(self) -> None:
        """消费配置页的「立即同步群列表」开关。

        配置页没有可点击按钮，保存配置会热重载本插件，所以用 bool 开关当触发器：
        加载时读到 true 就复位开关，然后等平台实例就绪，调用 aiocqhttp 的
        get_group_list 把群写进群表。
        """
        self._reset_sync_flag()

        if not self._aiocqhttp_instances() and not self._platform_instances():
            # 插件可能先于平台加载，给平台连接留一点时间
            for _ in range(SYNC_WAIT_SECONDS):
                await asyncio.sleep(1)
                if self._aiocqhttp_instances():
                    break

        instances = self._aiocqhttp_instances()
        if not instances:
            logger.warning(
                "[broadcast] 群列表同步已跳过：没有已加载的 aiocqhttp 实例"
                "（该功能仅 aiocqhttp 可用）"
            )
            return

        before = len(self._groups)
        for platform_id, instance in instances:
            await self._discover_aiocqhttp(instance, platform_id)
        added = len(self._groups) - before
        if added:
            self._save_registry()
        logger.info(
            f"[broadcast] 群列表同步完成：{len(instances)} 个 aiocqhttp 实例，"
            f"新增 {added} 个群，当前共 {len(self._groups)} 个群"
        )

    def _reset_sync_flag(self) -> None:
        """把触发开关复位，避免下次重载重复同步。"""
        try:
            if not isinstance(self.config, dict):
                return
            if not self.config.get("sync_groups_now"):
                return
            self.config["sync_groups_now"] = False
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
        except Exception as exc:
            logger.warning(f"[broadcast] 复位「立即同步」开关失败：{exc}")

    async def _discover_aiocqhttp(self, instance: Any, platform_id: str) -> bool:
        try:
            client = instance.get_client()
            if client is None:
                return False
            groups = await asyncio.wait_for(
                client.api.call_action("get_group_list"), timeout=5
            )
        except Exception as exc:
            logger.debug(f"[broadcast] get_group_list 失败：{exc}")
            return False

        changed = False
        for item in groups or []:
            try:
                group_id = str(item.get("group_id") or "")
            except Exception:
                continue
            if not group_id:
                continue
            changed |= self._upsert_group(
                f"{platform_id}:{GROUP_MESSAGE_TYPE}:{group_id}",
                group_name=str(item.get("group_name") or ""),
            )
        return changed

    def _resolve_targets(self) -> list[dict[str, Any]]:
        """按避开名单过滤后的广播目标。"""
        exclude = self._str_list_conf("exclude_umos")
        excluded_umos = {item for item in exclude if ":" in item}
        excluded_ids = {item for item in exclude if ":" not in item}

        targets: list[dict[str, Any]] = []
        for umo, record in self._groups.items():
            group_id = str(record.get("group_id") or "")
            if umo in excluded_umos:
                continue
            if group_id and group_id in excluded_ids:
                continue
            targets.append(record)
        targets.sort(
            key=lambda r: (
                str(r.get("platform_id") or ""),
                str(r.get("group_id") or ""),
            )
        )
        return targets

    # ------------------------------------------------------------------ #
    # 额度
    # ------------------------------------------------------------------ #
    def _today(self) -> str:
        tz = self._timezone()
        now = datetime.now(tz) if tz else datetime.now()
        return now.strftime("%Y-%m-%d")

    def _quota_used(self) -> int:
        today = self._today()
        if self._state.get("date") != today:
            self._state = {"date": today, "sent": 0}
        try:
            return int(self._state.get("sent") or 0)
        except (TypeError, ValueError):
            return 0

    def _quota_limit(self) -> int:
        return max(0, self._int_conf("daily_limit", DEFAULT_DAILY_LIMIT))

    def _quota_available(self) -> bool:
        limit = self._quota_limit()
        return limit <= 0 or self._quota_used() < limit

    def _bump_quota(self) -> None:
        self._state["date"] = self._today()
        self._state["sent"] = self._quota_used() + 1

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    async def _safe_send(self, umo: str, text: str) -> bool:
        ok, reason = await self._send_one(umo, text)
        if not ok:
            logger.warning(f"[broadcast] 发送到 {umo} 失败：{reason}")
        return ok

    async def _send_one(self, umo: str, text: str) -> tuple[bool, str]:
        """向单个 UMO 发送文本，返回 (是否成功, 失败原因)。"""
        try:
            platform_id = umo.split(":", 1)[0]
        except Exception:
            return False, "UMO 格式不合法"

        if self._get_platform(platform_id) is None:
            return False, "平台实例未加载（UMO 中的平台 ID 不存在）"

        timeout = max(1, self._int_conf("send_timeout", DEFAULT_TIMEOUT))
        try:
            chain = MessageChain().message(text)
            found = await asyncio.wait_for(
                self.context.send_message(umo, chain), timeout=timeout
            )
        except asyncio.TimeoutError:
            return False, f"发送超时（>{timeout}s）"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 网络/接口错误
            return False, f"{type(exc).__name__}: {exc}"[:120]

        if found is False:
            return False, "未找到匹配的平台实例"
        return True, ""

    # ------------------------------------------------------------------ #
    # 广播主流程（手动 / 定时共用）
    # ------------------------------------------------------------------ #
    async def do_broadcast(
        self,
        message: str,
        *,
        origin_umo: str | None = None,
        targets: list[dict[str, Any]] | None = None,
        source: str = "manual",
    ) -> BroadcastResult | None:
        """执行一次全群广播。

        Args:
            message: 要发送的文本。
            origin_umo: 结果回执发送到的会话；定时任务传 None。
            targets: 预设目标列表；为 None 时重新解析。
            source: manual / schedule，仅影响日志与回执标题。
        """
        message = (message or "").strip()
        if not message:
            return None

        if self._busy:
            if origin_umo:
                await self._safe_send(
                    origin_umo, "⏳ 已有广播任务正在执行，请稍后再试。"
                )
            return None

        self._busy = True
        started = time.monotonic()
        try:
            if targets is None:
                await self._discover_from_platforms()
                targets = self._resolve_targets()

            result = BroadcastResult(total=len(targets))
            if not targets:
                if origin_umo:
                    await self._safe_send(origin_umo, "⚠️ 没有可广播的群聊。")
                return result

            interval = max(0.0, self._float_conf("send_interval", DEFAULT_INTERVAL))
            limiter = RateLimiter(
                self._int_conf("max_per_minute", DEFAULT_MAX_PER_MINUTE)
            )
            logger.info(
                f"[broadcast] 开始广播（{source}）：{len(targets)} 个群，"
                f"间隔 {interval}s，限速 {limiter.max_per_minute or '不限'}/分钟"
            )

            for index, record in enumerate(targets):
                umo = str(record.get("umo") or "")
                if not umo:
                    continue

                if not self._quota_available():
                    result.quota_blocked = True
                    result.skipped.extend(
                        str(item.get("umo") or "") for item in targets[index:]
                    )
                    logger.warning(
                        f"[broadcast] 已达每日上限 {self._quota_limit()} 条，提前结束"
                    )
                    break

                self._ensure_group_scene(umo)
                await limiter.acquire()
                ok, reason = await self._send_one(umo, message)
                if ok:
                    result.success += 1
                    self._bump_quota()
                else:
                    result.failures.append((umo, reason))
                    logger.warning(f"[broadcast] {umo} 发送失败：{reason}")

                if interval > 0 and index < len(targets) - 1:
                    await asyncio.sleep(interval)

            result.elapsed = time.monotonic() - started
            self._persist_state()
            logger.info(
                f"[broadcast] 广播结束：目标 {result.total}，成功 {result.success}，"
                f"失败 {len(result.failures)}，跳过 {len(result.skipped)}，"
                f"耗时 {result.elapsed:.1f}s"
            )
            if origin_umo:
                await self._safe_send(origin_umo, self._format_report(result, source))
            return result
        finally:
            self._busy = False

    # ------------------------------------------------------------------ #
    # 回执文本
    # ------------------------------------------------------------------ #
    def _format_report(self, result: BroadcastResult, source: str) -> str:
        title = "📢 定时广播完成" if source == "schedule" else "📢 广播完成"
        lines = [
            title,
            f"目标群：{result.total}",
            f"成功：{result.success}",
            f"失败：{len(result.failures)}",
            f"耗时：{result.elapsed:.1f}s",
        ]
        if result.skipped:
            reason = "每日额度已用尽" if result.quota_blocked else "未发送"
            lines.append(f"跳过：{len(result.skipped)}（{reason}）")
        if result.failures:
            lines.append("")
            lines.append("失败群聊：")
            for umo, reason in result.failures[:15]:
                lines.append(f"· {umo} → {reason}")
            if len(result.failures) > 15:
                lines.append(f"……其余 {len(result.failures) - 15} 个已省略")
        return "\n".join(lines)

    def _empty_targets_hint(self) -> str:
        return (
            "⚠️ 还没有登记任何群聊，广播无法发送。\n\n"
            "群列表来源：\n"
            "1. 机器人在某个群收到过任意消息后，该群会被自动登记；\n"
            "2. 挂了 aiocqhttp（NapCat / Lagrange）时，可在插件配置里打开"
            "「立即同步群列表」并保存；\n"
            "3. 手动登记：/broadcast_add <平台ID>:GroupMessage:<群号>；\n"
            "4. 查看已登记群：/broadcast_list。"
        )

    def _eta_text(self, count: int) -> str:
        interval = max(0.0, self._float_conf("send_interval", DEFAULT_INTERVAL))
        per_minute = self._int_conf("max_per_minute", DEFAULT_MAX_PER_MINUTE)
        seconds = count * interval
        if per_minute > 0:
            seconds = max(seconds, count / per_minute * 60)
        if seconds >= 60:
            return f"{seconds / 60:.1f} 分钟"
        return f"{seconds:.0f} 秒"

    # ------------------------------------------------------------------ #
    # 定时任务
    # ------------------------------------------------------------------ #
    def _setup_scheduler(self) -> None:
        """根据配置重建定时任务；关闭时销毁调度器。"""
        if not self._bool_conf("enable_schedule", False):
            self._stop_scheduler()
            return

        if AsyncIOScheduler is None or CronTrigger is None:
            logger.error("[broadcast] 未安装 apscheduler，无法启用定时广播")
            return

        cron = str(self._conf("schedule_cron", DEFAULT_CRON) or "").strip()
        message = str(self._conf("schedule_message", "") or "").strip()
        if not cron:
            logger.warning("[broadcast] enable_schedule 已开启，但 schedule_cron 为空")
            return
        if not message:
            logger.warning(
                "[broadcast] enable_schedule 已开启，但 schedule_message 为空，"
                "到点不会发送内容"
            )

        timezone = self._timezone()
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=timezone)
        except Exception as exc:
            logger.error(f"[broadcast] Cron 表达式无效（{cron}）：{exc}")
            # 表达式写错时停掉旧任务，避免按过期配置在错误时间群发
            self._stop_scheduler()
            return

        if self._scheduler is None or not self._scheduler.running:
            self._scheduler = AsyncIOScheduler(timezone=timezone)
            self._scheduler.start()

        self._scheduler.add_job(
            self._run_scheduled_broadcast,
            trigger=trigger,
            id=SCHEDULE_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )
        logger.info(
            f"[broadcast] 定时广播已启用：{cron}，下次触发 {self._next_run_text()}"
        )

    def _stop_scheduler(self) -> None:
        if self._scheduler is None:
            return
        with suppress(Exception):
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
        self._scheduler = None

    def _next_run_text(self) -> str:
        if self._scheduler is None:
            return "未启用"
        try:
            job = self._scheduler.get_job(SCHEDULE_JOB_ID)
            next_run = getattr(job, "next_run_time", None) if job else None
        except Exception:
            next_run = None
        if not next_run:
            return "未排期"
        return next_run.strftime("%Y-%m-%d %H:%M:%S %Z")

    async def _run_scheduled_broadcast(self) -> None:
        message = str(self._conf("schedule_message", "") or "").strip()
        if not message:
            logger.warning("[broadcast] 定时广播触发，但 schedule_message 为空，已跳过")
            return
        logger.info("[broadcast] 定时广播触发")
        try:
            await self.do_broadcast(message, origin_umo=None, source="schedule")
        except Exception:
            logger.exception("[broadcast] 定时广播执行异常")

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #
    @staticmethod
    def _payload(event: AstrMessageEvent) -> str:
        """取出指令名之后的全部文本（保留空格与换行）。

        AstrBot 会把唤醒前缀从 message_str 中剥掉，所以这里只需去掉第一个词
        （即指令名本身）。这样即使参数解析只拿到第一个 token，也能还原完整内容。
        """
        raw = str(getattr(event, "message_str", "") or "").strip()
        if not raw:
            return ""
        match = re.match(r"^\S+\s*(.*)$", raw, re.DOTALL)
        return match.group(1).strip() if match else raw

    @filter.command("broadcast", alias={"广播"})
    async def broadcast(self, event: AstrMessageEvent, content: str = ""):
        """全群广播：/broadcast <消息内容>"""
        if not self._is_admin(event):
            yield event.plain_result(
                "⚠️ 权限不足：仅管理员可使用广播指令。\n"
                f"你的 UID：{self._sender_id(event)}\n"
                "（把该 UID 加入插件配置 admin_uids 即可使用）"
            )
            return

        message = self._payload(event) or str(content or "").strip()
        if not message:
            yield event.plain_result(
                "用法：/broadcast <消息内容>\n"
                "例如：/broadcast 今晚 20:00 例行维护，预计 10 分钟"
            )
            return

        if self._busy:
            yield event.plain_result("⏳ 已有广播任务正在执行，请稍后再试。")
            return

        # 把发起指令的这个群也登记进去（首次使用场景）
        if self._register_event_group(event):
            self._save_registry()

        await self._discover_from_platforms()
        targets = self._resolve_targets()
        if not targets:
            yield event.plain_result(self._empty_targets_hint())
            return

        used = self._quota_used()
        limit = self._quota_limit()
        if limit > 0 and used >= limit:
            yield event.plain_result(
                f"⚠️ 今日主动消息额度已用尽（{used}/{limit}），"
                "请明天再试或调整 daily_limit。"
            )
            return

        yield event.plain_result(
            f"🚀 开始广播\n目标群：{len(targets)}\n"
            f"预计耗时：{self._eta_text(len(targets))}\n"
            f"完成后会在本会话回执结果。"
        )

        origin = event.unified_msg_origin
        self._task = asyncio.create_task(
            self.do_broadcast(
                message, origin_umo=origin, targets=targets, source="manual"
            )
        )
        self._task.add_done_callback(self._on_task_done)

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"[broadcast] 广播任务异常：{exc!r}")

    @filter.command("broadcast_list")
    async def broadcast_list(self, event: AstrMessageEvent):
        """查看已登记的群聊列表"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 权限不足：仅管理员可用。")
            return

        await self._discover_from_platforms()
        targets = self._resolve_targets()
        if not targets:
            yield event.plain_result(self._empty_targets_hint())
            return

        lines = [f"📋 已登记群聊：{len(targets)} 个（已按避开名单过滤）", ""]
        for index, record in enumerate(targets, start=1):
            group_id = str(record.get("group_id") or "")
            platform_id = str(record.get("platform_id") or "")
            lines.append(f"{index}. [{platform_id}] {group_id}")
        excluded = len(self._groups) - len(targets)
        if excluded > 0:
            lines.append("")
            lines.append(f"另有 {excluded} 个群被避开名单跳过")
        yield event.plain_result("\n".join(lines))

    @filter.command("broadcast_add")
    async def broadcast_add(self, event: AstrMessageEvent, target: str = ""):
        """手动登记一个群：/broadcast_add <平台ID>:GroupMessage:<群号>"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 权限不足：仅管理员可用。")
            return

        arg = (self._payload(event) or str(target or "")).strip()
        if not arg:
            yield event.plain_result(
                "用法：/broadcast_add <平台ID>:GroupMessage:<群号>\n"
                "例如：/broadcast_add qq_official:GroupMessage:ABC123\n"
                f"当前已加载的平台：{self._platform_ids_text()}"
            )
            return

        parts = arg.split(":", 2)
        if len(parts) != 3 or not parts[0].strip() or not parts[2].strip():
            yield event.plain_result(
                "⚠️ UMO 格式不正确，必须写成 <平台ID>:GroupMessage:<群号>。\n"
                "例如：/broadcast_add telegram:GroupMessage:-1001234567890\n"
                f"当前已加载的平台：{self._platform_ids_text()}"
            )
            return

        platform_id, message_type, group_id = (part.strip() for part in parts)
        if message_type != GROUP_MESSAGE_TYPE:
            yield event.plain_result(
                f"⚠️ 只支持群聊会话，消息类型必须是 {GROUP_MESSAGE_TYPE}，"
                f"收到的是 `{message_type}`。"
            )
            return
        if self._get_platform(platform_id) is None:
            yield event.plain_result(
                f"⚠️ 平台 `{platform_id}` 未加载，请检查 UMO 第一段是否填写正确。\n"
                f"当前已加载的平台：{self._platform_ids_text()}"
            )
            return

        umo = f"{platform_id}:{GROUP_MESSAGE_TYPE}:{group_id}"
        self._upsert_group(umo)
        self._save_registry()
        yield event.plain_result(
            f"✅ 已登记群聊：{umo}\n当前共 {len(self._groups)} 个群。"
        )

    def _platform_ids_text(self) -> str:
        ids = []
        for instance in self._platform_instances():
            try:
                platform_id = str(getattr(instance.meta(), "id", "") or "")
            except Exception:
                continue
            if platform_id:
                ids.append(platform_id)
        return "、".join(ids) if ids else "（暂无）"

    @filter.command("broadcast_remove")
    async def broadcast_remove(self, event: AstrMessageEvent, target: str = ""):
        """移除一个群：/broadcast_remove <平台ID>:GroupMessage:<群号>"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 权限不足：仅管理员可用。")
            return

        arg = (self._payload(event) or str(target or "")).strip()
        if not arg:
            yield event.plain_result(
                "用法：/broadcast_remove <平台ID>:GroupMessage:<群号>\n"
                "例如：/broadcast_remove qq_official:GroupMessage:ABC123"
            )
            return

        if arg.count(":") >= 2:
            matched = [arg] if arg in self._groups else []
        else:
            # 纯群号只作为过滤条件，不推断平台；多平台同号时要求写全 UMO
            matched = [
                umo
                for umo, record in self._groups.items()
                if str(record.get("group_id") or "") == arg
            ]
            if len(matched) > 1:
                yield event.plain_result(
                    f"⚠️ 群号 `{arg}` 在多个平台下都存在，请用完整 UMO 指定：\n"
                    + "\n".join(f"· {umo}" for umo in matched)
                )
                return

        if not matched:
            yield event.plain_result(f"⚠️ 未找到匹配的群：{arg}")
            return

        for umo in matched:
            self._groups.pop(umo, None)
        self._save_registry()
        yield event.plain_result(
            "✅ 已移除：\n" + "\n".join(f"· {umo}" for umo in matched)
        )

    @filter.command("broadcast_status")
    async def broadcast_status(self, event: AstrMessageEvent):
        """查看广播插件状态"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 权限不足：仅管理员可用。")
            return

        limit = self._quota_limit()
        used = self._quota_used()
        quota = f"{used}/{limit}" if limit > 0 else f"{used}/不限"
        schedule_on = self._bool_conf("enable_schedule", False)
        lines = [
            "📡 广播插件状态",
            f"已登记群聊：{len(self._groups)}",
            f"避开名单：{len(self._str_list_conf('exclude_umos'))}",
            f"今日已发送：{quota}（{self._today()}）",
            f"发送间隔：{self._float_conf('send_interval', DEFAULT_INTERVAL)}s，"
            "限速："
            f"{self._int_conf('max_per_minute', DEFAULT_MAX_PER_MINUTE) or '不限'}"
            "/分钟",
            f"定时广播：{'开启' if schedule_on else '关闭'}",
            "群列表同步："
            + ("aiocqhttp 可用" if self._aiocqhttp_instances() else "未挂载 aiocqhttp"),
        ]
        if schedule_on:
            cron = str(self._conf("schedule_cron", DEFAULT_CRON) or "").strip()
            lines.append(f"Cron：{cron}")
            lines.append(f"下次触发：{self._next_run_text()}")
        yield event.plain_result("\n".join(lines))

    @filter.command("broadcast_reload")
    async def broadcast_reload(self, event: AstrMessageEvent):
        """按当前配置重建定时任务"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 权限不足：仅管理员可用。")
            return

        try:
            self._setup_scheduler()
        except Exception as exc:
            yield event.plain_result(f"❌ 重建定时任务失败：{exc}")
            return

        if self._bool_conf("enable_schedule", False):
            yield event.plain_result(
                f"✅ 定时任务已按当前配置重建\n"
                f"Cron：{self._conf('schedule_cron', DEFAULT_CRON)}\n"
                f"下次触发：{self._next_run_text()}"
            )
        else:
            yield event.plain_result("✅ 定时广播当前为关闭状态，已停止调度器。")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    async def record_group(self, event: AstrMessageEvent):
        """被动记录收到过消息的群，作为广播目标来源。"""
        try:
            if self._register_event_group(event):
                self._save_registry()
        except Exception as exc:
            logger.debug(f"[broadcast] 记录群聊失败：{exc}")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 全部加载完成后校准一次定时任务。"""
        try:
            self._setup_scheduler()
        except Exception:
            logger.exception("[broadcast] on_astrbot_loaded 重建定时任务失败")

    async def terminate(self):
        """插件卸载/停用时释放资源。"""
        self._stop_scheduler()
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._task
        self._save_registry()
        self._persist_state()
        logger.info("[broadcast] 插件已卸载")
