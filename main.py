"""AstrBot 全群广播插件（独立可视化配置台版）。

架构说明：
    v2 起配置不再堆在 ``_conf_schema.json`` 里，而是由插件自带的
    Pages 页面（``pages/settings/``）管理，数据落在
    ``data/plugin_data/astrbot_plugin_broadcast/settings.json``。
    ``_conf_schema.json`` 只保留一个全局启用开关。

指令：
    /broadcast <消息内容>   向目标会话广播（管理员）
    /broadcast_status       查看运行状态
    /broadcast_list         查看已发现的会话
    /broadcast_reload       重载配置并重建定时任务

说明：
    广播目标不再局限于群聊。插件会综合三处来源发现会话：
      1. 插件自身记录过的 UMO（收到过消息即记录）；
      2. 平台适配器内存中的会话缓存（如 QQ 官方的 scene 缓存）；
      3. AstrBot 数据库中的已知 UMO（conversations / umo_senders /
         umo_aliases，与 WebUI 会话列表同源）。
    QQ 官方开放平台没有“查询已加入群列表”的接口，因此仍无法凭空
    枚举全部群；挂载 aiocqhttp 时可在页面上点“刷新会话”调用
    ``get_group_list`` 补全。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

try:  # AstrBot >= 4.24.2 提供的插件 Web API 工具
    from astrbot.api import web as _web

    WEB_API_AVAILABLE = True
except Exception:  # pragma: no cover - 旧版本降级
    _web = None  # type: ignore[assignment]
    WEB_API_AVAILABLE = False

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    SCHEDULER_AVAILABLE = True
except Exception:  # pragma: no cover - 未安装依赖
    AsyncIOScheduler = None  # type: ignore[assignment]
    CronTrigger = None  # type: ignore[assignment]
    SCHEDULER_AVAILABLE = False

try:
    from sqlalchemy import text as sql_text

    SQLALCHEMY_AVAILABLE = True
except Exception:  # pragma: no cover - 未安装依赖
    sql_text = None  # type: ignore[assignment]
    SQLALCHEMY_AVAILABLE = False


PLUGIN_NAME = "astrbot_plugin_broadcast"
SCHEDULE_JOB_ID = "broadcast_cron"
GROUP_MESSAGE_TYPE = "GroupMessage"

DEFAULT_INTERVAL = 2.0
DEFAULT_MAX_PER_MINUTE = 20
DEFAULT_DAILY_LIMIT = 1000
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TIME = "09:00"
DEFAULT_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6]

MAX_INTERVAL = 60.0
MAX_MESSAGE_LENGTH = 4000
REGISTRY_FLUSH_DELAY = 5.0
DB_UMO_LIMIT = 5000

#: 这些平台不是真正的 IM 会话，广播时应忽略。
NON_BROADCAST_PLATFORMS = {"webchat", "unknown", ""}

WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_UMO_RE = re.compile(r"^[^:\s]+:[^:\s]+:.+$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class SettingsError(ValueError):
    """配置校验失败，消息可直接展示给用户。"""


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #
@dataclass
class ScheduleSettings:
    """定时广播的可视化配置（由后端编译成 Cron）。"""

    mode: str = "daily"  # daily | weekly
    time: str = DEFAULT_TIME
    weekdays: list[int] = field(default_factory=lambda: list(DEFAULT_WEEKDAYS))

    def to_cron(self) -> str:
        hour, minute = self.time.split(":")
        if self.mode == "weekly":
            days = sorted({d for d in self.weekdays if 0 <= d <= 6})
            if not days:
                raise SettingsError("每周模式至少要选择一天")
            day_of_week = ",".join(str(d) for d in days)
        else:
            day_of_week = "*"
        return f"{int(minute)} {int(hour)} * * {day_of_week}"

    def describe(self) -> str:
        if self.mode == "weekly":
            days = "、".join(WEEKDAY_LABELS[d] for d in sorted(set(self.weekdays)))
            return f"每周 {days} {self.time}"
        return f"每天 {self.time}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "time": self.time,
            "weekdays": list(self.weekdays),
        }

    @classmethod
    def from_dict(cls, raw: Any, base: "ScheduleSettings | None" = None):
        base = base or cls()
        if raw is None:
            return base
        if not isinstance(raw, dict):
            raise SettingsError("schedule 必须是对象")
        mode = str(raw.get("mode", base.mode) or base.mode).strip().lower()
        if mode not in {"daily", "weekly"}:
            raise SettingsError("定时模式只支持 daily 或 weekly")
        time_str = str(raw.get("time", base.time) or base.time).strip()
        if not _TIME_RE.match(time_str):
            raise SettingsError(f"时间格式无效：{time_str}（应为 HH:MM）")
        raw_days = raw.get("weekdays", base.weekdays)
        days: list[int] = []
        if isinstance(raw_days, list):
            for item in raw_days:
                try:
                    day = int(item)
                except (TypeError, ValueError):
                    continue
                if 0 <= day <= 6 and day not in days:
                    days.append(day)
        days.sort()
        if mode == "weekly" and not days:
            raise SettingsError("每周模式至少要选择一天")
        return cls(mode=mode, time=time_str, weekdays=days or list(DEFAULT_WEEKDAYS))


@dataclass
class Settings:
    """插件运行配置（持久化到 settings.json）。"""

    admin_uids: list[str] = field(default_factory=list)
    target_umos: list[str] = field(default_factory=list)
    exclude_umos: list[str] = field(default_factory=list)
    send_interval: float = DEFAULT_INTERVAL
    max_per_minute: int = DEFAULT_MAX_PER_MINUTE
    daily_limit: int = DEFAULT_DAILY_LIMIT
    enable_schedule: bool = False
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    schedule_message: str = ""
    timezone: str = DEFAULT_TIMEZONE
    auto_register: bool = True

    # -- 序列化 ---------------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "admin_uids": list(self.admin_uids),
            "target_umos": list(self.target_umos),
            "exclude_umos": list(self.exclude_umos),
            "send_interval": self.send_interval,
            "max_per_minute": self.max_per_minute,
            "daily_limit": self.daily_limit,
            "enable_schedule": self.enable_schedule,
            "schedule": self.schedule.to_dict(),
            "schedule_message": self.schedule_message,
            "timezone": self.timezone,
            "auto_register": self.auto_register,
        }

    @classmethod
    def from_dict(cls, raw: Any, base: "Settings | None" = None) -> "Settings":
        base = base or cls()
        if not isinstance(raw, dict):
            raise SettingsError("配置必须是 JSON 对象")

        admin_uids = _parse_str_list(raw.get("admin_uids", base.admin_uids))
        target = _parse_umo_list(
            raw.get("target_umos", base.target_umos), "target_umos"
        )
        exclude = _parse_umo_list(
            raw.get("exclude_umos", base.exclude_umos), "exclude_umos"
        )

        interval = _parse_float(
            raw.get("send_interval", base.send_interval), "发送间隔"
        )
        if interval < 0 or interval > MAX_INTERVAL:
            raise SettingsError(f"发送间隔需在 0~{MAX_INTERVAL:g} 秒之间")

        max_per_minute = _parse_int(
            raw.get("max_per_minute", base.max_per_minute), "每分钟上限"
        )
        if max_per_minute < 0 or max_per_minute > 60:
            raise SettingsError("每分钟上限需在 0~60 之间（0 表示不限）")

        daily_limit = _parse_int(raw.get("daily_limit", base.daily_limit), "每日额度")
        if daily_limit < 0 or daily_limit > 1_000_000:
            raise SettingsError("每日额度需在 0~1000000 之间（0 表示不限）")

        message = str(raw.get("schedule_message", base.schedule_message) or "")
        if len(message) > MAX_MESSAGE_LENGTH:
            raise SettingsError(f"定时消息不能超过 {MAX_MESSAGE_LENGTH} 字")

        timezone = str(raw.get("timezone", base.timezone) or DEFAULT_TIMEZONE).strip()
        if not _valid_timezone(timezone):
            raise SettingsError(f"时区无效：{timezone}")

        schedule = ScheduleSettings.from_dict(
            raw.get("schedule", base.schedule.to_dict()), base.schedule
        )

        return cls(
            admin_uids=admin_uids,
            target_umos=target,
            exclude_umos=exclude,
            send_interval=round(interval, 2),
            max_per_minute=max_per_minute,
            daily_limit=daily_limit,
            enable_schedule=bool(raw.get("enable_schedule", base.enable_schedule)),
            schedule=schedule,
            schedule_message=message,
            timezone=timezone,
            auto_register=bool(raw.get("auto_register", base.auto_register)),
        )

    def replace(self, **kwargs: Any) -> "Settings":
        data = self.to_dict()
        for key, value in kwargs.items():
            if key == "schedule" and isinstance(value, ScheduleSettings):
                data["schedule"] = value.to_dict()
            else:
                data[key] = value
        return Settings.from_dict(data)


def _parse_str_list(raw: Any) -> list[str]:
    items: list[str] = []
    if isinstance(raw, str):
        raw = re.split(r"[\s,，;；]+", raw)
    if not isinstance(raw, list):
        return items
    for item in raw:
        value = str(item or "").strip()
        if value and value not in items:
            items.append(value)
    return items


def _parse_umo_list(raw: Any, field_name: str) -> list[str]:
    items: list[str] = []
    if isinstance(raw, str):
        raw = [line.strip() for line in raw.splitlines()]
    if not isinstance(raw, list):
        return items
    for item in raw:
        value = str(item or "").strip()
        if not value:
            continue
        if not _UMO_RE.match(value):
            raise SettingsError(
                f"{field_name} 中的「{value}」不是合法 UMO，"
                "应形如 平台ID:GroupMessage:会话ID"
            )
        if value not in items:
            items.append(value)
    return items


def _parse_float(raw: Any, label: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{label}必须是数字") from exc


def _parse_int(raw: Any, label: str) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{label}必须是整数") from exc


def _valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 会话与广播结果
# --------------------------------------------------------------------------- #
@dataclass
class SessionInfo:
    """一个可广播的会话。"""

    umo: str
    platform: str
    message_type: str
    session_id: str
    display_name: str = ""
    adapter: str = ""
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "umo": self.umo,
            "platform": self.platform,
            "adapter": self.adapter or self.platform,
            "message_type": self.message_type,
            "session_id": self.session_id,
            "display_name": self.display_name or self.session_id,
            "sources": list(self.sources),
        }


@dataclass
class BroadcastResult:
    """一次广播的统计结果。"""

    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    duration: float = 0.0
    source: str = "manual"
    failures: list[tuple[str, str]] = field(default_factory=list)
    missing_targets: list[str] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return self.success + self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "skipped": self.skipped,
            "duration": round(self.duration, 2),
            "source": self.source,
            "failures": [
                {"umo": umo, "reason": reason} for umo, reason in self.failures[:20]
            ],
            "missing_targets": list(self.missing_targets[:20]),
        }


class RateLimiter:
    """滑动窗口限速器，把发送频率压到每分钟 N 条以内。"""

    def __init__(self, max_per_minute: int) -> None:
        self.limit = max(0, int(max_per_minute))
        self._stamps: deque[float] = deque()

    async def acquire(self) -> None:
        if self.limit <= 0:
            return
        while True:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] >= 60:
                self._stamps.popleft()
            if len(self._stamps) < self.limit:
                self._stamps.append(now)
                return
            wait = 60 - (now - self._stamps[0])
            await asyncio.sleep(max(wait, 0.05))


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
class OfficialBroadcastPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._data_dir = self._resolve_data_dir()
        self._settings_path = self._data_dir / "settings.json"
        self._registry_path = self._data_dir / "sessions.json"
        self._state_path = self._data_dir / "state.json"
        self._legacy_registry_path = self._data_dir / "groups.json"

        self._settings = Settings()
        self._registry: dict[str, dict[str, Any]] = {}
        self._state: dict[str, Any] = {"date": "", "sent": 0, "last_result": None}
        self._busy = False
        self._task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        self._scheduler: Any = None
        self._schedule_error = ""

        self._load_settings()
        self._load_registry()
        self._load_state()
        self._register_web_apis()

        # 插件热重载时 __init__ 也要重建定时任务，不能只靠 on_astrbot_loaded
        try:
            ok, message = self._reschedule()
            if ok:
                logger.info(f"[broadcast] {message}")
            elif self._settings.enable_schedule:
                logger.warning(f"[broadcast] 定时任务未生效：{message}")
        except Exception:
            logger.exception("[broadcast] 初始化定时任务失败")

    # ------------------------------------------------------------------ #
    # 基础工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_data_dir() -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            return Path("data") / "plugin_data" / PLUGIN_NAME

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self._settings.timezone)
        except Exception:
            return ZoneInfo(DEFAULT_TIMEZONE)

    def _today(self) -> str:
        return datetime.now(self._tz()).strftime("%Y-%m-%d")

    def _config_enabled(self) -> bool:
        try:
            return bool(self.config.get("enabled", True))
        except Exception:
            return True

    @staticmethod
    def _atomic_write(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def _backup_broken(self, path: Path) -> None:
        try:
            backup = path.with_suffix(path.suffix + f".broken.{int(time.time())}")
            path.replace(backup)
            logger.warning(f"[broadcast] 配置文件损坏，已备份到 {backup}")
        except Exception:
            logger.warning("[broadcast] 配置文件损坏且备份失败，将使用默认配置")

    # ------------------------------------------------------------------ #
    # 配置读写
    # ------------------------------------------------------------------ #
    def _load_settings(self) -> None:
        if not self._settings_path.exists():
            self._settings = self._migrate_legacy_config()
            try:
                self._atomic_write(self._settings_path, self._settings.to_dict())
                logger.info("[broadcast] 已生成默认配置 settings.json")
            except Exception:
                logger.exception("[broadcast] 写入默认配置失败")
            return

        try:
            raw = json.loads(self._settings_path.read_text(encoding="utf-8"))
            self._settings = Settings.from_dict(raw)
        except SettingsError as exc:
            logger.error(f"[broadcast] 配置校验失败，回退默认值：{exc}")
            self._backup_broken(self._settings_path)
            self._settings = Settings()
        except Exception:
            logger.exception("[broadcast] 读取配置失败，回退默认值")
            self._backup_broken(self._settings_path)
            self._settings = Settings()

    def _migrate_legacy_config(self) -> Settings:
        """把旧版 _conf_schema.json 里的字段一次性迁移到 settings.json。"""
        settings = Settings()
        try:
            raw = dict(self.config) if self.config else {}
        except Exception:
            return settings
        if not raw:
            return settings
        try:
            settings.admin_uids = _parse_str_list(raw.get("admin_uids", []))
            settings.exclude_umos = _parse_umo_list(
                raw.get("exclude_umos", []), "exclude_umos"
            )
            settings.send_interval = min(
                max(float(raw.get("send_interval", DEFAULT_INTERVAL)), 0.0),
                MAX_INTERVAL,
            )
            settings.max_per_minute = max(
                0, min(int(raw.get("max_per_minute", DEFAULT_MAX_PER_MINUTE)), 60)
            )
            settings.daily_limit = max(
                0, int(raw.get("daily_limit", DEFAULT_DAILY_LIMIT))
            )
            settings.enable_schedule = bool(raw.get("enable_schedule", False))
            settings.schedule_message = str(raw.get("schedule_message", "") or "")
            settings.timezone = str(raw.get("timezone", DEFAULT_TIMEZONE))
            settings.auto_register = bool(raw.get("auto_discover", True))
            cron = str(raw.get("schedule_cron", "") or "").strip()
            if cron:
                parsed = _cron_to_schedule(cron)
                if parsed:
                    settings.schedule = parsed
            logger.info("[broadcast] 已从旧版插件配置迁移设置")
        except Exception:
            logger.warning("[broadcast] 旧配置迁移失败，使用默认设置", exc_info=True)
        return settings

    def _save_settings(self) -> None:
        self._atomic_write(self._settings_path, self._settings.to_dict())

    def _set_enabled(self, value: bool) -> None:
        try:
            self.config["enabled"] = bool(value)
        except Exception:
            return
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception:
                logger.warning("[broadcast] 写入插件总开关失败", exc_info=True)

    # ------------------------------------------------------------------ #
    # 会话登记表
    # ------------------------------------------------------------------ #
    def _load_registry(self) -> None:
        path = self._registry_path
        if not path.exists() and self._legacy_registry_path.exists():
            path = self._legacy_registry_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._backup_broken(path)
            return
        if not isinstance(raw, dict):
            return
        for umo, item in raw.items():
            if not isinstance(umo, str) or not _UMO_RE.match(umo):
                continue
            record = item if isinstance(item, dict) else {}
            self._registry[umo] = {
                "name": str(record.get("name") or ""),
                "last_seen": float(record.get("last_seen") or 0.0),
            }
        logger.info(f"[broadcast] 已加载 {len(self._registry)} 条会话记录")

    def _save_registry(self) -> None:
        try:
            self._atomic_write(self._registry_path, self._registry)
        except Exception:
            logger.warning("[broadcast] 写入会话登记表失败", exc_info=True)

    def _record_session(self, umo: str, name: str = "") -> bool:
        if not _UMO_RE.match(umo or ""):
            return False
        record = self._registry.setdefault(umo, {"name": "", "last_seen": 0.0})
        changed = False
        if name and record.get("name") != name:
            record["name"] = name
            changed = True
        now = time.time()
        if now - float(record.get("last_seen") or 0) > 60:
            record["last_seen"] = now
            changed = True
        return changed

    def _schedule_flush(self) -> None:
        """合并短时间内的多次登记，避免每条消息都写盘。"""
        if self._flush_task and not self._flush_task.done():
            return
        loop = asyncio.get_event_loop()
        self._flush_task = loop.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(REGISTRY_FLUSH_DELAY)
            self._save_registry()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[broadcast] 延迟写入会话表失败", exc_info=True)

    # ------------------------------------------------------------------ #
    # 运行状态
    # ------------------------------------------------------------------ #
    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            self._backup_broken(self._state_path)
            return
        if isinstance(raw, dict):
            self._state = {
                "date": str(raw.get("date") or ""),
                "sent": int(raw.get("sent") or 0),
                "last_result": raw.get("last_result"),
            }

    def _save_state(self) -> None:
        try:
            self._atomic_write(self._state_path, self._state)
        except Exception:
            logger.warning("[broadcast] 写入运行状态失败", exc_info=True)

    def _quota_used(self) -> int:
        if self._state.get("date") != self._today():
            self._state = {"date": self._today(), "sent": 0, "last_result": None}
        return int(self._state.get("sent") or 0)

    def _quota_consume(self, count: int = 1) -> None:
        used = self._quota_used()
        self._state["date"] = self._today()
        self._state["sent"] = used + count
        self._save_state()

    # ------------------------------------------------------------------ #
    # 会话发现
    # ------------------------------------------------------------------ #
    async def _collect_sessions(self, refresh: bool = False) -> list[SessionInfo]:
        """汇总所有已知会话。

        Args:
            refresh: 是否额外向 aiocqhttp 协议端拉取群/好友列表。
        """
        found: dict[str, SessionInfo] = {}
        adapter_names = self._adapter_name_map()

        def add(umo: str, name: str = "", source: str = "") -> None:
            if not _UMO_RE.match(umo or ""):
                return
            platform, message_type, session_id = _split_umo(umo)
            if platform in NON_BROADCAST_PLATFORMS:
                return
            item = found.get(umo)
            if item is None:
                item = SessionInfo(
                    umo=umo,
                    platform=platform,
                    message_type=message_type,
                    session_id=session_id,
                    adapter=adapter_names.get(platform, ""),
                )
                found[umo] = item
            if name and not item.display_name:
                item.display_name = name
            if source and source not in item.sources:
                item.sources.append(source)

        for umo, record in self._registry.items():
            add(umo, str(record.get("name") or ""), "registry")

        for session in self._adapter_sessions():
            add(session["umo"], session.get("name", ""), "adapter")

        for umo, name in await self._db_sessions():
            add(umo, name, "database")

        if refresh:
            for session in await self._sync_aiocqhttp():
                add(session["umo"], session.get("name", ""), "aiocqhttp")

        return sorted(
            found.values(),
            key=lambda s: (s.platform, s.message_type, s.display_name or s.session_id),
        )

    def _adapter_name_map(self) -> dict[str, str]:
        """平台实例 ID -> 适配器类型名（如 qq_official / aiocqhttp）。"""
        try:
            instances = self.context.platform_manager.get_insts()
        except Exception:
            return {}
        mapping: dict[str, str] = {}
        for instance in instances:
            try:
                meta = instance.meta()
            except Exception:
                continue
            pid = str(getattr(meta, "id", "") or "")
            name = str(getattr(meta, "name", "") or "")
            if pid and name:
                mapping[pid] = name
        return mapping

    def _adapter_sessions(self) -> list[dict[str, str]]:
        """读取平台适配器内存中的会话缓存（只做鸭子类型探测）。"""
        try:
            instances = self.context.platform_manager.get_insts()
        except Exception:
            return []
        result: list[dict[str, str]] = []
        for instance in instances:
            try:
                platform_id = str(instance.meta().id or "")
            except Exception:
                continue
            scenes = getattr(instance, "_session_scene", None)
            if not isinstance(scenes, dict):
                continue
            for session_id, scene in list(scenes.items()):
                if scene == "group":
                    message_type = GROUP_MESSAGE_TYPE
                elif scene in {"friend", "private"}:
                    message_type = "FriendMessage"
                else:
                    continue
                result.append({"umo": f"{platform_id}:{message_type}:{session_id}"})
        return result

    async def _db_sessions(self) -> list[tuple[str, str]]:
        """从 AstrBot 数据库读取已知 UMO（与 WebUI 会话列表同源）。

        三张表分别查询：旧版本可能还没有 umo_aliases，单表缺失不应
        影响其它来源。
        """
        if not SQLALCHEMY_AVAILABLE:
            return []
        try:
            db = self.context.get_db()
        except Exception:
            return []
        if db is None:
            return []

        queries = (
            "SELECT DISTINCT user_id FROM conversations "
            "WHERE user_id IS NOT NULL AND user_id != ''",
            "SELECT DISTINCT umo FROM umo_senders WHERE umo IS NOT NULL AND umo != ''",
            "SELECT DISTINCT umo FROM umo_aliases WHERE umo IS NOT NULL AND umo != ''",
        )
        seen: dict[str, None] = {}
        for sql in queries:
            try:
                async with db.get_db() as session:
                    result = await session.execute(sql_text(sql))
                    for row in result.fetchall():
                        umo = str(row[0])
                        if umo:
                            seen.setdefault(umo, None)
            except Exception as exc:
                logger.debug(f"[broadcast] 查询会话来源失败：{exc}")
            if len(seen) >= DB_UMO_LIMIT:
                break

        names: dict[str, str] = {}
        try:
            async with db.get_db() as session:
                result = await session.execute(
                    sql_text(
                        "SELECT umo, COALESCE(NULLIF(user_alias, ''), "
                        "NULLIF(auto_name, ''), '') FROM umo_aliases"
                    )
                )
                for row in result.fetchall():
                    umo, name = str(row[0]), str(row[1] or "")
                    if name:
                        names[umo] = name
        except Exception:
            pass

        umos = list(seen)[:DB_UMO_LIMIT]
        return [(umo, names.get(umo, "")) for umo in umos]

    def _aiocqhttp_instances(self) -> list[tuple[str, Any]]:
        try:
            instances = self.context.platform_manager.get_insts()
        except Exception:
            return []
        result: list[tuple[str, Any]] = []
        for instance in instances:
            try:
                meta = instance.meta()
            except Exception:
                continue
            if str(getattr(meta, "name", "") or "") == "aiocqhttp":
                result.append((str(getattr(meta, "id", "") or ""), instance))
        return result

    async def _sync_aiocqhttp(self) -> list[dict[str, str]]:
        """向 aiocqhttp 协议端拉取群与好友列表（仅页面刷新时调用）。"""
        collected: list[dict[str, str]] = []
        for platform_id, instance in self._aiocqhttp_instances():
            client = None
            try:
                client = instance.get_client()
            except Exception:
                client = None
            if client is None:
                continue

            for action, message_type in (
                ("get_group_list", GROUP_MESSAGE_TYPE),
                ("get_friend_list", "FriendMessage"),
            ):
                try:
                    rows = await asyncio.wait_for(
                        client.api.call_action(action), timeout=8
                    )
                except Exception as exc:
                    logger.debug(f"[broadcast] {action} 失败：{exc}")
                    continue
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    sid = row.get("group_id") or row.get("user_id")
                    if sid is None:
                        continue
                    name = str(row.get("group_name") or row.get("nickname") or "")
                    umo = f"{platform_id}:{message_type}:{sid}"
                    collected.append({"umo": umo, "name": name})
                    self._record_session(umo, name)
        if collected:
            self._save_registry()
        return collected

    # ------------------------------------------------------------------ #
    # 目标解析
    # ------------------------------------------------------------------ #
    async def _resolve_targets(
        self, settings: Settings, refresh: bool = False
    ) -> tuple[list[SessionInfo], list[str]]:
        """返回 (实际发送目标, 白名单中未发现的 UMO)。"""
        sessions = await self._collect_sessions(refresh=refresh)
        index = {s.umo: s for s in sessions}
        exclude = set(settings.exclude_umos)

        if settings.target_umos:
            targets: list[SessionInfo] = []
            missing: list[str] = []
            for umo in settings.target_umos:
                if umo in exclude:
                    continue
                info = index.get(umo)
                if info is None:
                    platform, message_type, session_id = _split_umo(umo)
                    info = SessionInfo(
                        umo=umo,
                        platform=platform,
                        message_type=message_type,
                        session_id=session_id,
                        display_name=session_id,
                    )
                    missing.append(umo)
                targets.append(info)
            return targets, missing

        return [s for s in sessions if s.umo not in exclude], []

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    def _ensure_group_scene(self, session: SessionInfo) -> None:
        """预热 QQ 官方适配器的会话场景缓存。

        QQ 官方适配器在缺少 msg_id 时，只有 ``_session_scene`` 记录为
        group 才允许主动群发；该缓存在重启后为空，会导致消息被静默丢弃。
        """
        if session.message_type != GROUP_MESSAGE_TYPE:
            return
        try:
            platform = self.context.get_platform_inst(session.platform)
        except Exception:
            return
        scenes = getattr(platform, "_session_scene", None) if platform else None
        if isinstance(scenes, dict):
            scenes.setdefault(session.session_id, "group")

    async def _send_one(self, session: SessionInfo, message: str) -> tuple[bool, str]:
        self._ensure_group_scene(session)
        try:
            found = await self.context.send_message(
                session.umo, MessageChain().message(message)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if found is False:
            return False, "未找到对应的平台实例"
        return True, ""

    async def do_broadcast(
        self, message: str, *, origin_umo: str = "", source: str = "manual"
    ) -> BroadcastResult | None:
        """执行一次广播。已有任务在跑时返回 None。"""
        if self._busy:
            return None

        message = (message or "").strip()
        if not message:
            return BroadcastResult(source=source)
        if not self._config_enabled():
            return BroadcastResult(source=source)

        self._busy = True
        started = time.monotonic()
        result = BroadcastResult(source=source)
        try:
            settings = self._settings
            targets, missing = await self._resolve_targets(settings)
            result.total = len(targets)
            result.missing_targets = missing
            if not targets:
                return result

            limiter = RateLimiter(settings.max_per_minute)
            daily_limit = settings.daily_limit
            interval = settings.send_interval

            for index, session in enumerate(targets):
                if daily_limit > 0 and self._quota_used() >= daily_limit:
                    result.skipped = len(targets) - index
                    logger.warning(
                        f"[broadcast] 今日额度已用尽（{daily_limit}），"
                        f"剩余 {result.skipped} 个会话跳过"
                    )
                    break

                await limiter.acquire()
                ok, reason = await self._send_one(session, message)
                self._quota_consume(1)
                if ok:
                    result.success += 1
                else:
                    result.failed += 1
                    result.failures.append((session.umo, reason))
                    logger.warning(f"[broadcast] 发送失败 {session.umo}：{reason}")

                if interval > 0 and index < len(targets) - 1:
                    await asyncio.sleep(interval)

            result.duration = time.monotonic() - started
            self._state["last_result"] = {
                "time": datetime.now(self._tz()).isoformat(timespec="seconds"),
                "source": source,
                "total": result.total,
                "success": result.success,
                "failed": result.failed,
                "skipped": result.skipped,
            }
            self._save_state()
            logger.info(
                f"[broadcast] 完成：成功 {result.success}，失败 {result.failed}，"
                f"跳过 {result.skipped}，耗时 {result.duration:.1f}s"
            )
            return result
        finally:
            self._busy = False

    # ------------------------------------------------------------------ #
    # 定时任务
    # ------------------------------------------------------------------ #
    def _shutdown_scheduler(self) -> None:
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is None:
            return
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            logger.debug("[broadcast] 关闭调度器时出错", exc_info=True)

    def _reschedule(self) -> tuple[bool, str]:
        """清掉旧任务并按最新配置重新注册。"""
        self._shutdown_scheduler()
        self._schedule_error = ""

        if not self._settings.enable_schedule:
            return False, "定时广播未开启"
        if not self._settings.schedule_message.strip():
            self._schedule_error = "定时消息为空，到点不会发送"
            return False, self._schedule_error
        if not self._config_enabled():
            self._schedule_error = "插件总开关已关闭"
            return False, self._schedule_error
        if not SCHEDULER_AVAILABLE:
            self._schedule_error = "未安装 apscheduler，无法注册定时任务"
            return False, self._schedule_error

        try:
            cron = self._settings.schedule.to_cron()
        except SettingsError as exc:
            self._schedule_error = str(exc)
            return False, self._schedule_error

        try:
            trigger = CronTrigger.from_crontab(cron, timezone=self._tz())
            scheduler = AsyncIOScheduler(timezone=self._tz())
            scheduler.add_job(
                self._run_scheduled,
                trigger,
                id=SCHEDULE_JOB_ID,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=600,
            )
            scheduler.start()
        except Exception as exc:
            self._schedule_error = f"注册定时任务失败：{exc}"
            logger.exception("[broadcast] 注册定时任务失败")
            return False, self._schedule_error

        self._scheduler = scheduler
        return True, f"定时广播已生效（{self._settings.schedule.describe()}）"

    async def _run_scheduled(self) -> None:
        message = self._settings.schedule_message.strip()
        if not message:
            logger.warning("[broadcast] 定时任务触发，但消息为空，已跳过")
            return
        if self._busy:
            logger.warning("[broadcast] 上一次广播仍在执行，本次定时任务跳过")
            return
        logger.info("[broadcast] 定时广播开始")
        await self.do_broadcast(message, source="schedule")

    def _next_run_at(self) -> str:
        if self._scheduler is None:
            return ""
        try:
            job = self._scheduler.get_job(SCHEDULE_JOB_ID)
            next_run = getattr(job, "next_run_time", None) if job else None
            return next_run.isoformat() if next_run else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    # Web API
    # ------------------------------------------------------------------ #
    def _register_web_apis(self) -> None:
        if not WEB_API_AVAILABLE:
            logger.warning(
                "[broadcast] 当前 AstrBot 版本不支持 astrbot.api.web，"
                "可视化配置页不可用（需要 4.24.2 及以上）"
            )
            return
        routes = (
            (
                f"/{PLUGIN_NAME}/config/get",
                self.api_get_config,
                ["GET"],
                "读取广播配置",
            ),
            (
                f"/{PLUGIN_NAME}/config/save",
                self.api_save_config,
                ["POST"],
                "保存广播配置",
            ),
            (
                f"/{PLUGIN_NAME}/config/refresh",
                self.api_refresh_sessions,
                ["POST"],
                "刷新会话列表",
            ),
        )
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(route, handler, methods, desc)
            except Exception:
                logger.exception(f"[broadcast] 注册 Web API 失败：{route}")

    def _overview(self) -> dict[str, Any]:
        used = self._quota_used()
        return {
            "enabled": self._config_enabled(),
            "today_sent": used,
            "daily_limit": self._settings.daily_limit,
            "quota_left": (
                max(self._settings.daily_limit - used, 0)
                if self._settings.daily_limit > 0
                else -1
            ),
            "busy": self._busy,
            "schedule_active": self._scheduler is not None,
            "schedule_error": self._schedule_error,
            "next_run_at": self._next_run_at(),
            "cron": self._settings.schedule.to_cron()
            if self._settings.enable_schedule
            else "",
            "last_result": self._state.get("last_result"),
            "web_api": WEB_API_AVAILABLE,
            "scheduler_available": SCHEDULER_AVAILABLE,
            "aiocqhttp": [pid for pid, _ in self._aiocqhttp_instances()],
        }

    async def _api_payload(self) -> dict[str, Any]:
        sessions = await self._collect_sessions(refresh=False)
        settings = self._settings
        target = set(settings.target_umos)
        exclude = set(settings.exclude_umos)
        eligible = [
            s
            for s in sessions
            if s.umo not in exclude and (not target or s.umo in target)
        ]
        return {
            "settings": settings.to_dict(),
            "sessions": [s.to_dict() for s in sessions],
            "platforms": sorted({s.platform for s in sessions}),
            "message_types": sorted({s.message_type for s in sessions}),
            "overview": self._overview(),
            "summary": {
                "total": len(sessions),
                "selected": len(settings.target_umos),
                "excluded": len(settings.exclude_umos),
                "eligible": len(eligible),
                "mode": "targeted" if settings.target_umos else "all",
            },
        }

    async def api_get_config(self):
        try:
            return _web.json_response(await self._api_payload())
        except Exception as exc:
            logger.exception("[broadcast] 读取配置失败")
            return _web.error_response(f"读取配置失败：{exc}", status_code=500)

    async def api_save_config(self):
        try:
            payload = await _web.request.json(default={})
        except Exception as exc:
            return _web.error_response(f"请求体解析失败：{exc}")
        if not isinstance(payload, dict):
            return _web.error_response("请求体必须是 JSON 对象")

        try:
            settings = Settings.from_dict(payload, base=self._settings)
        except SettingsError as exc:
            return _web.error_response(str(exc))

        self._settings = settings
        try:
            self._save_settings()
        except Exception as exc:
            logger.exception("[broadcast] 保存配置失败")
            return _web.error_response(f"写入配置文件失败：{exc}", status_code=500)

        if "enabled" in payload:
            self._set_enabled(bool(payload.get("enabled")))

        ok, message = self._reschedule()
        if not ok and settings.enable_schedule:
            logger.warning(f"[broadcast] 定时任务未生效：{message}")

        data = await self._api_payload()
        data["schedule_message"] = message
        data["schedule_ok"] = ok
        return _web.json_response(data)

    async def api_refresh_sessions(self):
        try:
            payload = await _web.request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        refresh_remote = bool(payload.get("remote", True))
        try:
            sessions = await self._collect_sessions(refresh=refresh_remote)
        except Exception as exc:
            logger.exception("[broadcast] 刷新会话失败")
            return _web.error_response(f"刷新会话失败：{exc}", status_code=500)
        return _web.json_response(
            {
                "sessions": [s.to_dict() for s in sessions],
                "platforms": sorted({s.platform for s in sessions}),
                "message_types": sorted({s.message_type for s in sessions}),
                "overview": self._overview(),
            }
        )

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #
    @filter.command("broadcast")
    async def broadcast(self, event: AstrMessageEvent):
        """向目标会话广播一条消息。"""
        if not self._config_enabled():
            yield event.plain_result("⚠️ 广播插件已在配置中禁用。")
            return
        if not self._is_admin(event):
            yield event.plain_result(
                "⚠️ 权限不足：仅管理员可使用广播指令。\n"
                f"你的 UID：{self._sender_id(event) or '未知'}\n"
                "可在插件配置页把它加入管理员列表。"
            )
            return

        message = self._payload(event)
        if not message:
            yield event.plain_result(
                "用法：/broadcast <消息内容>\n"
                "例如：/broadcast 今晚 20:00 例行维护，预计 10 分钟"
            )
            return

        if len(message) > MAX_MESSAGE_LENGTH:
            yield event.plain_result(
                f"⚠️ 消息过长，请控制在 {MAX_MESSAGE_LENGTH} 字以内。"
            )
            return

        if self._busy:
            yield event.plain_result("⚠️ 已有广播任务在执行，请等待完成后再试。")
            return

        targets, _ = await self._resolve_targets(self._settings)
        if not targets:
            yield event.plain_result(await self._empty_targets_hint())
            return

        used = self._quota_used()
        limit = self._settings.daily_limit
        if limit > 0 and used >= limit:
            yield event.plain_result(
                f"⚠️ 今日主动消息额度已用尽（{used}/{limit}），"
                "请明天再试或调整每日额度。"
            )
            return

        estimated = int(len(targets) * (self._settings.send_interval + 0.35))
        yield event.plain_result(
            f"📢 开始广播，目标 {len(targets)} 个会话，预计耗时约 {estimated} 秒……"
        )

        origin = str(getattr(event, "unified_msg_origin", "") or "")
        self._task = asyncio.create_task(
            self._broadcast_and_report(message, origin, "manual")
        )

    async def _broadcast_and_report(
        self, message: str, origin_umo: str, source: str
    ) -> None:
        try:
            result = await self.do_broadcast(
                message, origin_umo=origin_umo, source=source
            )
        except Exception as exc:
            logger.exception("[broadcast] 广播任务异常")
            if origin_umo:
                await self._reply(origin_umo, f"❌ 广播失败：{exc}")
            return

        if result is None:
            if origin_umo:
                await self._reply(origin_umo, "⚠️ 已有广播任务在执行，本次未发送。")
            return
        if origin_umo:
            await self._reply(origin_umo, self._format_report(result))

    async def _reply(self, umo: str, text: str) -> None:
        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception:
            logger.warning(f"[broadcast] 回执发送失败：{umo}", exc_info=True)

    @filter.command("broadcast_status")
    async def broadcast_status(self, event: AstrMessageEvent):
        """查看广播运行状态。"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 权限不足：仅管理员可用。")
            return

        settings = self._settings
        overview = self._overview()
        sessions = await self._collect_sessions()
        targets, missing = await self._resolve_targets(settings)
        lines = [
            "📊 广播运行状态",
            f"插件开关：{'启用' if overview['enabled'] else '禁用'}",
            f"已知会话：{len(sessions)} 个｜本次目标：{len(targets)} 个",
            f"目标模式：{'白名单' if settings.target_umos else '全部会话'}",
            f"排除名单：{len(settings.exclude_umos)} 个",
            f"发送间隔：{settings.send_interval:g}s｜"
            f"限速：{settings.max_per_minute or '不限'}/分钟",
            f"今日已发：{overview['today_sent']}"
            + (
                f" / {settings.daily_limit}" if settings.daily_limit > 0 else "（不限）"
            ),
            f"定时广播：{'开启' if settings.enable_schedule else '关闭'}",
        ]
        if settings.enable_schedule:
            lines.append(f"计划：{settings.schedule.describe()}")
            lines.append(f"下次触发：{overview['next_run_at'] or '未注册'}")
            if overview["schedule_error"]:
                lines.append(f"⚠️ {overview['schedule_error']}")
        if missing:
            lines.append(f"⚠️ 白名单中 {len(missing)} 个 UMO 当前未发现")
        if not overview["web_api"]:
            lines.append("⚠️ 当前 AstrBot 版本不支持可视化配置页")
        yield event.plain_result("\n".join(lines))

    @filter.command("broadcast_list")
    async def broadcast_list(self, event: AstrMessageEvent):
        """列出已发现的会话。"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 权限不足：仅管理员可用。")
            return

        sessions = await self._collect_sessions()
        if not sessions:
            yield event.plain_result(await self._empty_targets_hint())
            return

        target = set(self._settings.target_umos)
        exclude = set(self._settings.exclude_umos)
        lines = [f"📋 已发现 {len(sessions)} 个会话（最多显示 40 条）"]
        for session in sessions[:40]:
            mark = (
                "🚫"
                if session.umo in exclude
                else ("✅" if not target or session.umo in target else "⬜")
            )
            lines.append(f"{mark} {session.display_name}｜{session.umo}")
        if len(sessions) > 40:
            lines.append(f"…… 其余 {len(sessions) - 40} 条请到插件配置页查看")
        yield event.plain_result("\n".join(lines))

    @filter.command("broadcast_reload")
    async def broadcast_reload(self, event: AstrMessageEvent):
        """重载配置并重建定时任务。"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 权限不足：仅管理员可用。")
            return
        self._load_settings()
        ok, message = self._reschedule()
        yield event.plain_result(
            ("✅ " if ok else "ℹ️ ")
            + message
            + f"\n会话数：{len(await self._collect_sessions())}"
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def record_group(self, event: AstrMessageEvent):
        """收到任意消息时登记会话，便于后续广播。"""
        if not self._settings.auto_register:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        if self._record_session(umo, self._session_name(event)):
            self._schedule_flush()

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "").strip()
        except Exception:
            return ""

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        uid = self._sender_id(event)
        admins = self._settings.admin_uids
        if admins:
            return bool(uid) and uid in admins
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    @staticmethod
    def _session_name(event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        group = getattr(message_obj, "group", None)
        name = str(getattr(group, "group_name", "") or "") if group else ""
        if name:
            return name
        getter = getattr(event, "get_sender_name", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:
                return ""
        return ""

    @staticmethod
    def _payload(event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "")
        return re.sub(r"^\s*/\S+\s*", "", text, count=1, flags=re.DOTALL).strip()

    async def _empty_targets_hint(self) -> str:
        sessions = await self._collect_sessions()
        if not sessions:
            return (
                "⚠️ 还没有发现任何会话，广播无法发送。\n\n"
                "插件会从三个来源发现会话：\n"
                "1. 机器人收到过消息的会话会被自动登记；\n"
                "2. 平台适配器内存中的会话缓存；\n"
                "3. AstrBot 数据库中的已知会话。\n\n"
                "如果仍然为空：\n"
                "· 让机器人在目标群/私聊里各收一条消息；\n"
                "· 或挂载 aiocqhttp 后在插件配置页点「刷新会话」；\n"
                "· 或在配置页把完整 UMO 手动加入白名单。"
            )
        return (
            f"已知 {len(sessions)} 个会话，但都被白名单/排除名单过滤掉了。\n"
            "请到插件配置页检查目标会话设置。"
        )

    def _format_report(self, result: BroadcastResult) -> str:
        lines = [
            "📢 广播完成",
            f"目标：{result.total} 个｜成功：{result.success}｜失败：{result.failed}",
        ]
        if result.skipped:
            lines.append(f"跳过：{result.skipped}（今日额度已用尽）")
        if result.duration:
            lines.append(f"耗时：{result.duration:.1f} 秒")
        if result.missing_targets:
            lines.append(
                f"⚠️ 白名单中 {len(result.missing_targets)} 个 UMO 未在会话列表中发现，"
                "已尝试直接发送"
            )
        if result.failures:
            lines.append("失败明细（最多 10 条）：")
            for umo, reason in result.failures[:10]:
                lines.append(f"· {umo} → {reason}")
            if len(result.failures) > 10:
                lines.append(f"…… 另有 {len(result.failures) - 10} 条失败未显示")
        return "\n".join(lines)

    async def terminate(self):
        """插件卸载/重载时清理定时器与后台任务。"""
        self._shutdown_scheduler()
        for task in (self._task, self._flush_task):
            if task and not task.done():
                task.cancel()
        self._save_registry()
        self._save_state()


def _split_umo(umo: str) -> tuple[str, str, str]:
    parts = umo.split(":", 2)
    if len(parts) < 3:
        return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "", umo)
    return parts[0], parts[1], parts[2]


def _cron_to_schedule(cron: str) -> ScheduleSettings | None:
    """把常见的 5 段 Cron 反解成可视化配置（迁移旧配置用）。"""
    fields = cron.split()
    if len(fields) != 5:
        return None
    minute, hour, dom, month, dow = fields
    if not (minute.isdigit() and hour.isdigit()):
        return None
    if dom != "*" or month != "*":
        return None
    time_str = f"{int(hour):02d}:{int(minute):02d}"
    if dow == "*":
        return ScheduleSettings(mode="daily", time=time_str)
    days: list[int] = []
    for item in dow.split(","):
        part = item.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            days.append(int(part))
    if not days:
        return None
    return ScheduleSettings(mode="weekly", time=time_str, weekdays=sorted(set(days)))
