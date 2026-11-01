"""AstrBot 全群广播插件（独立可视化配置台版）。

架构说明：
    插件没有 ``_conf_schema.json``：启停由 AstrBot 插件管理统一负责，
    其余配置全部由自带的可视化视图（``views/settings/``）管理，
    数据落在 ``data/plugin_data/astrbot_plugin_broadcast/settings.json``。

    视图通过 ``window.AstrBotPluginView`` bridge 调用下列后端接口，
    接口一律用 ``astrbot.api.web`` 的 ``request`` / ``json_response`` /
    ``error_response`` 读写，不直接接触底层 Web 框架对象。

指令：
    /broadcast <消息内容>   向目标会话广播（管理员）
    /broadcast_status       查看运行状态
    /broadcast_list         查看已发现的会话
    /broadcast_reload       重载配置并重建定时任务
    /broadcast_auth @某人   把被 @ 的用户添加为广播管理员（管理员/超管）

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
import secrets
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
from astrbot.api.web import error_response, json_response, request

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger

    SCHEDULER_AVAILABLE = True
except Exception:  # pragma: no cover - 未安装依赖
    AsyncIOScheduler = None  # type: ignore[assignment]
    CronTrigger = None  # type: ignore[assignment]
    DateTrigger = None  # type: ignore[assignment]
    SCHEDULER_AVAILABLE = False

try:
    from sqlalchemy import text as sql_text

    SQLALCHEMY_AVAILABLE = True
except Exception:  # pragma: no cover - 未安装依赖
    sql_text = None  # type: ignore[assignment]
    SQLALCHEMY_AVAILABLE = False


PLUGIN_NAME = "astrbot_plugin_broadcast"
JOB_PREFIX = "broadcast_task_"
GROUP_MESSAGE_TYPE = "GroupMessage"

DEFAULT_INTERVAL = 2.0
DEFAULT_MAX_PER_MINUTE = 20
DEFAULT_DAILY_LIMIT = 1000
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TIME = "09:00"

MAX_INTERVAL = 60.0
MAX_MESSAGE_LENGTH = 4000
MAX_SCHEDULES = 20
#: 广播进行中到点的定时任务最多排这么多，避免极端情况下无限堆积。
#: 任务总数上限是 20，同一天里每个任务最多排一次，32 足够宽裕。
MAX_PENDING = 32
DB_UMO_LIMIT = 5000

#: 这些平台不是真正的 IM 会话，广播时应忽略。
NON_BROADCAST_PLATFORMS = {"webchat", "unknown", ""}

WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WEEKDAY_CRON = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_UMO_RE = re.compile(r"^[^:\s]+:[^:\s]+:.+$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# 各平台在纯文本里遗留的 @ 标记：QQ 官方用 <qqbot-at-user id="..."> 或 <@openid>
_AT_MARKUP_RE = re.compile(
    r'<qqbot-at-user\s+id="(?P<a>[^"]+)"\s*/?>|<@!?(?P<b>[^>\s]+)>'
)
# 文本形式的 @昵称 / @123456
_AT_TEXT_RE = re.compile(r"[@＠]\S*")


class SettingsError(ValueError):
    """配置校验失败，消息可直接展示给用户。"""


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #
@dataclass
class ScheduleTask:
    """一条定时广播计划。

    ``mode`` 决定重复方式：
        daily   每天 HH:MM
        weekly  每周选中的星期 HH:MM
        once    指定日期 HH:MM 只执行一次（像闹钟，触发后自动停用）
    """

    id: str = ""
    message: str = ""
    enabled: bool = True
    mode: str = "daily"
    time: str = DEFAULT_TIME
    weekdays: list[int] = field(default_factory=list)
    date: str = ""
    last_fired: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = new_task_id()

    @property
    def job_id(self) -> str:
        return f"{JOB_PREFIX}{self.id}"

    def describe(self) -> str:
        if self.mode == "weekly":
            days = "、".join(WEEKDAY_LABELS[d] for d in sorted(set(self.weekdays)))
            return f"每周 {days} {self.time}"
        if self.mode == "once":
            return f"{self.date or '未选日期'} {self.time} 仅一次"
        return f"每天 {self.time}"

    def run_at(self, tz: Any) -> datetime | None:
        """一次性任务的下次触发时刻；其他模式返回 None。"""
        if self.mode != "once" or not self.date:
            return None
        hour, minute = (int(part) for part in self.time.split(":"))
        year, month, day = (int(part) for part in self.date.split("-"))
        return datetime(year, month, day, hour, minute, tzinfo=tz)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "message": self.message,
            "enabled": self.enabled,
            "mode": self.mode,
            "time": self.time,
            "weekdays": list(self.weekdays),
            "date": self.date,
            "last_fired": self.last_fired,
        }

    @classmethod
    def from_dict(cls, raw: Any, base: ScheduleTask | None = None) -> ScheduleTask:
        base = base or cls()
        if not isinstance(raw, dict):
            raise SettingsError("定时任务必须是对象")

        task_id = str(raw.get("id", base.id) or "").strip()
        if not _ID_RE.match(task_id):
            task_id = new_task_id()

        message = str(raw.get("message", base.message) or "")
        if len(message) > MAX_MESSAGE_LENGTH:
            raise SettingsError(f"定时消息不能超过 {MAX_MESSAGE_LENGTH} 字")

        mode = str(raw.get("mode", base.mode) or base.mode).strip().lower()
        if mode not in {"daily", "weekly", "once"}:
            raise SettingsError("重复方式只支持 daily / weekly / once")

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

        date_str = str(raw.get("date", base.date) or "").strip()
        if mode == "once":
            if not _DATE_RE.match(date_str):
                raise SettingsError("仅一次任务需要选择日期（YYYY-MM-DD）")
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError as exc:
                raise SettingsError(f"日期无效：{date_str}") from exc

        last_fired = str(raw.get("last_fired", base.last_fired) or "")

        return cls(
            id=task_id,
            message=message,
            enabled=bool(raw.get("enabled", base.enabled)),
            mode=mode,
            time=time_str,
            weekdays=days,
            date=date_str,
            last_fired=last_fired,
        )


def new_task_id() -> str:
    """生成短且可读的任务 ID。"""
    return f"t{secrets.token_hex(3)}"


def _parse_schedules(raw: Any, base: list[ScheduleTask]) -> list[ScheduleTask]:
    if raw is None:
        return list(base)
    if not isinstance(raw, list):
        raise SettingsError("schedules 必须是数组")
    if len(raw) > MAX_SCHEDULES:
        raise SettingsError(f"最多只能添加 {MAX_SCHEDULES} 个定时任务")
    tasks: list[ScheduleTask] = []
    seen: set[str] = set()
    for item in raw:
        task = ScheduleTask.from_dict(item)
        if task.id in seen:
            task.id = new_task_id()
        seen.add(task.id)
        tasks.append(task)
    return tasks


@dataclass
class Settings:
    """插件运行配置（持久化到 settings.json）。"""

    admin_uids: list[str] = field(default_factory=list)
    target_umos: list[str] = field(default_factory=list)
    seen_umos: list[str] = field(default_factory=list)
    auto_select_new: bool = True
    send_interval: float = DEFAULT_INTERVAL
    max_per_minute: int = DEFAULT_MAX_PER_MINUTE
    daily_limit: int = DEFAULT_DAILY_LIMIT
    schedules: list[ScheduleTask] = field(default_factory=list)
    timezone: str = DEFAULT_TIMEZONE

    # -- 序列化 ---------------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "admin_uids": list(self.admin_uids),
            "target_umos": list(self.target_umos),
            "seen_umos": list(self.seen_umos),
            "auto_select_new": self.auto_select_new,
            "send_interval": self.send_interval,
            "max_per_minute": self.max_per_minute,
            "daily_limit": self.daily_limit,
            "schedules": [task.to_dict() for task in self.schedules],
            "timezone": self.timezone,
        }

    @classmethod
    def from_dict(cls, raw: Any, base: Settings | None = None) -> Settings:
        base = base or cls()
        if not isinstance(raw, dict):
            raise SettingsError("配置必须是 JSON 对象")

        admin_uids = _parse_str_list(raw.get("admin_uids", base.admin_uids))
        target = _parse_umo_list(
            raw.get("target_umos", base.target_umos), "target_umos"
        )
        if "seen_umos" in raw:
            seen = _parse_umo_list(raw.get("seen_umos"), "seen_umos")
        else:
            # 旧配置没有 seen_umos：把排除名单原样挪进来，
            # 升级后这些会话不会被「自动勾选」重新捡回去。
            seen = _parse_umo_list(raw.get("exclude_umos"), "exclude_umos")

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

        timezone = str(raw.get("timezone", base.timezone) or DEFAULT_TIMEZONE).strip()
        if not _valid_timezone(timezone):
            raise SettingsError(f"时区无效：{timezone}")

        return cls(
            admin_uids=admin_uids,
            target_umos=target,
            seen_umos=list(dict.fromkeys([*base.seen_umos, *seen])),
            auto_select_new=_parse_bool(
                raw.get("auto_select_new", base.auto_select_new), base.auto_select_new
            ),
            send_interval=round(interval, 2),
            max_per_minute=max_per_minute,
            daily_limit=daily_limit,
            schedules=_parse_schedules(raw.get("schedules"), base.schedules),
            timezone=timezone,
        )

    def replace(self, **kwargs: Any) -> Settings:
        data = self.to_dict()
        for key, value in kwargs.items():
            if key == "schedules" and value is not None:
                data["schedules"] = [task.to_dict() for task in value]
            else:
                data[key] = value
        return Settings.from_dict(data)


def _parse_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in {"true", "1", "yes", "on"}:
            return True
        if value in {"false", "0", "no", "off"}:
            return False
    if isinstance(raw, (int, float)):
        return bool(raw)
    return default


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


def _short_time(iso: str) -> str:
    """把 ISO 时间压成「MM-DD HH:MM」，取不到就原样返回。"""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%m-%d %H:%M")
    except ValueError:
        return iso


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
        self._data_dir = self._resolve_data_dir()
        self._settings_path = self._data_dir / "settings.json"
        self._state_path = self._data_dir / "state.json"

        self._settings = Settings()
        self._state: dict[str, Any] = {"date": "", "sent": 0, "last_result": None}
        self._busy = False
        self._idle: asyncio.Event | None = None
        self._pending: deque[str] = deque()
        self._drain_task: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._scheduler: Any = None
        self._schedule_error = ""
        self._task_errors: dict[str, str] = {}

        self._load_settings()
        self._load_state()
        self._register_web_apis()

        # 插件热重载时 __init__ 也要重建定时任务，不能只靠 on_astrbot_loaded
        try:
            ok, message = self._reschedule()
            if ok:
                logger.info(f"[broadcast] {message}")
            elif self._settings.schedules:
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
        """读取 settings.json；不存在时直接落一份默认配置。"""
        if not self._settings_path.exists():
            self._settings = Settings()
            try:
                self._atomic_write(self._settings_path, self._settings.to_dict())
                logger.info("[broadcast] 已生成默认配置 settings.json")
            except Exception:
                logger.exception("[broadcast] 写入默认配置失败")
            return

        try:
            raw = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and (
                "schedule" in raw
                or "enable_schedule" in raw
                or "schedule_message" in raw
            ):
                logger.warning(
                    "[broadcast] settings.json 仍是 2.1 及更早版本的单任务结构，"
                    "定时设置未加载；请到配置页重新添加定时任务"
                )
            self._settings = Settings.from_dict(raw)
        except SettingsError as exc:
            logger.error(f"[broadcast] 配置校验失败，回退默认值：{exc}")
            self._backup_broken(self._settings_path)
            self._settings = Settings()
        except Exception:
            logger.exception("[broadcast] 读取配置失败，回退默认值")
            self._backup_broken(self._settings_path)
            self._settings = Settings()

    def _save_settings(self) -> None:
        self._atomic_write(self._settings_path, self._settings.to_dict())

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
        return collected

    # ------------------------------------------------------------------ #
    # 目标解析
    # ------------------------------------------------------------------ #
    def _adopt_sessions(self, sessions: list[SessionInfo]) -> list[str]:
        """把首次发现的会话自动加入广播名单，返回新增的 UMO。

        ``seen_umos`` 记录出现过的会话，因此手动取消勾选的会话
        不会在下次刷新时被重新勾上；只有真正的新会话才会自动选中。
        关闭 ``auto_select_new`` 后仍会记录 ``seen_umos``，只是不勾选，
        这样重新打开开关时不会把历史会话当成新会话一次性补勾。
        """
        seen = set(self._settings.seen_umos)
        fresh = [s.umo for s in sessions if s.umo not in seen]
        if not fresh:
            return []

        self._settings.seen_umos = list(
            dict.fromkeys([*self._settings.seen_umos, *(s.umo for s in sessions)])
        )
        added: list[str] = []
        if self._settings.auto_select_new:
            targets = list(self._settings.target_umos)
            for umo in fresh:
                if umo not in targets:
                    targets.append(umo)
                    added.append(umo)
            self._settings.target_umos = targets
        self._save_settings()
        if added:
            logger.info(f"[broadcast] 自动勾选 {len(added)} 个新会话")
        return added

    async def _resolve_targets(
        self, settings: Settings, refresh: bool = False
    ) -> tuple[list[SessionInfo], list[str]]:
        """返回 (实际发送目标, 已知会话中找不到的 UMO)。"""
        sessions = await self._collect_sessions(refresh=refresh)
        self._adopt_sessions(sessions)
        settings = self._settings
        index = {s.umo: s for s in sessions}

        targets: list[SessionInfo] = []
        missing: list[str] = []
        for umo in settings.target_umos:
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

        self._busy = True
        self._idle_event().clear()
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
            self._idle_event().set()
            self._kick_pending()

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

    def _build_trigger(self, task: ScheduleTask) -> Any:
        """把任务编译成 APScheduler 触发器。"""
        tz = self._tz()
        hour, minute = (int(part) for part in task.time.split(":"))
        if task.mode == "once":
            run_at = task.run_at(tz)
            if run_at is None:
                raise SettingsError("仅一次任务缺少日期")
            if run_at <= datetime.now(tz):
                raise SettingsError("计划时间已过，请重新选择日期")
            return DateTrigger(run_date=run_at)
        if task.mode == "weekly":
            days = sorted({d for d in task.weekdays if 0 <= d <= 6})
            if not days:
                raise SettingsError("每周模式至少要选择一天")
            day_of_week = ",".join(WEEKDAY_CRON[d] for d in days)
            return CronTrigger(
                day_of_week=day_of_week, hour=hour, minute=minute, timezone=tz
            )
        return CronTrigger(hour=hour, minute=minute, timezone=tz)

    def _reschedule(self) -> tuple[bool, str]:
        """清掉旧任务，按最新配置为每个任务注册独立 job。"""
        self._shutdown_scheduler()
        self._pending.clear()
        self._schedule_error = ""
        self._task_errors = {}
        tasks = self._settings.schedules
        enabled = [task for task in tasks if task.enabled]

        if not tasks:
            return False, "尚未添加定时任务"
        if not enabled:
            return False, "所有定时任务都已停用"
        if not SCHEDULER_AVAILABLE:
            self._schedule_error = "未安装 apscheduler，无法注册定时任务"
            return False, self._schedule_error

        scheduler = AsyncIOScheduler(timezone=self._tz())
        registered = 0
        for task in enabled:
            try:
                trigger = self._build_trigger(task)
            except SettingsError as exc:
                self._task_errors[task.id] = str(exc)
                continue
            except Exception as exc:  # pragma: no cover - 触发器异常
                self._task_errors[task.id] = f"时间规则无效：{exc}"
                continue
            try:
                scheduler.add_job(
                    self._run_scheduled_task,
                    trigger,
                    args=[task.id],
                    id=task.job_id,
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=600,
                )
                registered += 1
            except Exception as exc:  # pragma: no cover - 注册异常
                self._task_errors[task.id] = f"注册失败：{exc}"

        if registered == 0:
            detail = "；".join(sorted(set(self._task_errors.values())))
            self._schedule_error = (
                f"没有可用的定时任务：{detail}" if detail else ("没有可用的定时任务")
            )
            return False, self._schedule_error

        try:
            scheduler.start()
        except Exception as exc:
            self._schedule_error = f"启动调度器失败：{exc}"
            logger.exception("[broadcast] 启动调度器失败")
            return False, self._schedule_error

        self._scheduler = scheduler
        failed = len(enabled) - registered
        message = f"已注册 {registered} 个定时任务"
        if failed:
            message += f"，{failed} 个未生效"
        return True, message

    async def _run_scheduled_task(self, task_id: str) -> None:
        task = next((t for t in self._settings.schedules if t.id == task_id), None)
        if task is None or not task.enabled:
            return

        if not task.message.strip():
            self._task_errors[task_id] = "消息内容为空，未发送"
            logger.warning(f"[broadcast] 定时任务 {task_id} 消息为空，已跳过")
            self._finish_once_task(task, fired=False)
            return

        if self._busy:
            self._queue_task(task)
            return

        await self._execute_task(task)

    async def _execute_task(self, task: ScheduleTask) -> None:
        """真正执行一条定时任务；一次性任务无论成败都在之后停用。"""
        logger.info(f"[broadcast] 定时任务 {task.id} 开始（{task.describe()}）")
        error = ""
        try:
            result = await self.do_broadcast(task.message.strip(), source="schedule")
            if result is None:
                # 极端情况：刚被别的广播抢占，重新排队，等它跑完立刻补发
                self._queue_task(task)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"执行失败：{exc}"
            logger.exception(f"[broadcast] 定时任务 {task.id} 执行失败")

        if error:
            self._task_errors[task.id] = error
        else:
            self._task_errors.pop(task.id, None)
        self._finish_once_task(task, fired=not error)

    def _idle_event(self) -> asyncio.Event:
        """当前广播结束的信号；懒创建，避免在无事件循环时构造。"""
        if self._idle is None:
            self._idle = asyncio.Event()
            self._idle.set()
        return self._idle

    def _queue_task(self, task: ScheduleTask) -> None:
        """广播进行中到点的任务先排进队列，等当前广播结束立刻补发。"""
        if task.id in self._pending:
            return
        if len(self._pending) >= MAX_PENDING:
            self._task_errors[task.id] = "等待队列已满，本次未发送"
            logger.warning(f"[broadcast] 等待队列已满，任务 {task.id} 被丢弃")
            return
        self._pending.append(task.id)
        logger.info(f"[broadcast] 任务 {task.id} 到点时正在广播，已排队等待补发")
        self._kick_pending()

    def _kick_pending(self) -> None:
        """确保有且只有一个 drain 循环在等待空闲。"""
        if not self._pending:
            return
        current = self._drain_task
        if current is not None and not current.done():
            return
        try:
            self._drain_task = asyncio.create_task(self._drain_pending())
        except RuntimeError:  # pragma: no cover - 无事件循环
            logger.debug("[broadcast] 没有事件循环，排队任务暂不处理")

    async def _drain_pending(self) -> None:
        """当前广播一结束，就按到点先后把排队的任务依次补发。"""
        try:
            while self._pending:
                task_id = self._pending[0]
                task = next(
                    (t for t in self._settings.schedules if t.id == task_id), None
                )
                if task is None or not task.enabled or not task.message.strip():
                    self._pending.popleft()
                    continue
                if self._busy:
                    await self._idle_event().wait()
                    continue
                self._pending.popleft()
                await self._execute_task(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[broadcast] 处理排队任务失败")

    def _finish_once_task(self, task: ScheduleTask, *, fired: bool) -> None:
        """一次性任务跑完后自动停用，像闹钟响过就不再响。"""
        if task.mode != "once":
            return
        task.enabled = False
        if fired:
            task.last_fired = datetime.now(self._tz()).isoformat(timespec="seconds")
        try:
            self._save_settings()
        except Exception:
            logger.exception("[broadcast] 停用一次性任务失败")

    def _task_next_run(self, task_id: str) -> str:
        if self._scheduler is None:
            return ""
        try:
            job = self._scheduler.get_job(f"{JOB_PREFIX}{task_id}")
            next_run = getattr(job, "next_run_time", None) if job else None
            return next_run.isoformat() if next_run else ""
        except Exception:
            return ""

    def _schedule_runtime(self) -> list[dict[str, Any]]:
        """每条任务的运行态：下次触发时间与错误原因。"""
        runtime: list[dict[str, Any]] = []
        for task in self._settings.schedules:
            runtime.append(
                {
                    "id": task.id,
                    "next_run_at": self._task_next_run(task.id),
                    "pending": task.id in self._pending,
                    "error": self._task_errors.get(task.id, ""),
                }
            )
        return runtime

    def _next_run_at(self) -> str:
        """所有任务中最早的一次触发时间。"""
        stamps = [
            item["next_run_at"]
            for item in self._schedule_runtime()
            if item.get("next_run_at")
        ]
        return min(stamps) if stamps else ""

    # ------------------------------------------------------------------ #
    # Web API
    # ------------------------------------------------------------------ #
    def _register_web_apis(self) -> None:
        """注册视图后端接口。

        路由必须带插件名前缀；视图侧的 endpoint 不带前缀，由
        Dashboard 转发到 ``/api/v1/plugins/extensions/<plugin>/...``。
        """
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
            "schedules": self._schedule_runtime(),
            "last_result": self._state.get("last_result"),
            "scheduler_available": SCHEDULER_AVAILABLE,
            "aiocqhttp": [pid for pid, _ in self._aiocqhttp_instances()],
        }

    async def _api_payload(self) -> dict[str, Any]:
        sessions = await self._collect_sessions(refresh=False)
        self._adopt_sessions(sessions)
        settings = self._settings
        target = set(settings.target_umos)
        eligible = [s for s in sessions if s.umo in target]
        return {
            "settings": settings.to_dict(),
            "sessions": [s.to_dict() for s in sessions],
            "platforms": sorted({s.platform for s in sessions}),
            "message_types": sorted({s.message_type for s in sessions}),
            "overview": self._overview(),
            "summary": {
                "total": len(sessions),
                "selected": len(settings.target_umos),
                "eligible": len(eligible),
            },
        }

    async def api_get_config(self):
        """GET config/get：返回配置、会话列表与运行概览。"""
        try:
            return json_response(await self._api_payload())
        except Exception as exc:
            logger.exception("[broadcast] 读取配置失败")
            return error_response(f"读取配置失败：{exc}", status_code=500)

    async def api_save_config(self):
        """POST config/save：校验并保存配置，随即重建定时任务。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        try:
            settings = Settings.from_dict(payload, base=self._settings)
        except SettingsError as exc:
            return error_response(str(exc))

        self._settings = settings
        try:
            self._save_settings()
        except Exception as exc:
            logger.exception("[broadcast] 保存配置失败")
            return error_response(f"写入配置文件失败：{exc}", status_code=500)

        ok, message = self._reschedule()
        if not ok and settings.schedules:
            logger.warning(f"[broadcast] 定时任务未生效：{message}")

        data = await self._api_payload()
        data["schedule_note"] = message
        data["schedule_ok"] = ok
        return json_response(data)

    async def api_refresh_sessions(self):
        """POST config/refresh：重新收集会话，可顺带向 aiocqhttp 拉取。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            payload = {}
        refresh_remote = bool(payload.get("remote", True))
        try:
            sessions = await self._collect_sessions(refresh=refresh_remote)
        except Exception as exc:
            logger.exception("[broadcast] 刷新会话失败")
            return error_response(f"刷新会话失败：{exc}", status_code=500)
        return json_response(
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
            f"已知会话：{len(sessions)} 个｜本次目标：{len(targets)} 个",
            f"已勾选：{len(settings.target_umos)} 个会话",
            f"发送间隔：{settings.send_interval:g}s｜"
            f"限速：{settings.max_per_minute or '不限'}/分钟",
            f"今日已发：{overview['today_sent']}"
            + (
                f" / {settings.daily_limit}" if settings.daily_limit > 0 else "（不限）"
            ),
        ]

        tasks = settings.schedules
        active = sum(1 for task in tasks if task.enabled)
        lines.append(f"定时任务：{len(tasks)} 个（启用 {active} 个）")
        runtime = {item["id"]: item for item in overview["schedules"]}
        for task in tasks[:5]:
            state = runtime.get(task.id) or {}
            if state.get("error"):
                detail = f"⚠️ {state['error']}"
            elif state.get("next_run_at"):
                detail = f"下次 {_short_time(state['next_run_at'])}"
            elif task.last_fired:
                detail = f"已于 {_short_time(task.last_fired)} 执行"
            else:
                detail = "未注册"
            lines.append(f"{'✅' if task.enabled else '⏸'} {task.describe()}｜{detail}")
        if len(tasks) > 5:
            lines.append(f"…… 其余 {len(tasks) - 5} 条请到插件配置页查看")
        if overview["schedule_error"]:
            lines.append(f"⚠️ {overview['schedule_error']}")
        if missing:
            lines.append(f"⚠️ 有 {len(missing)} 个已勾选的 UMO 当前未发现")
        yield event.plain_result("\n".join(lines))

    @filter.command("broadcast_list")
    async def broadcast_list(self, event: AstrMessageEvent):
        """列出已发现的会话。"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 权限不足：仅管理员可用。")
            return

        sessions = await self._collect_sessions()
        self._adopt_sessions(sessions)
        if not sessions:
            yield event.plain_result(await self._empty_targets_hint())
            return

        target = set(self._settings.target_umos)
        lines = [f"📋 已发现 {len(sessions)} 个会话（✅ 已勾选，最多显示 40 条）"]
        for session in sessions[:40]:
            mark = "✅" if session.umo in target else "⬜"
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

    @filter.command("broadcast_auth")
    async def broadcast_auth(self, event: AstrMessageEvent):
        """把被 @ 的用户添加为广播管理员。"""
        if not self._can_grant_admin(event):
            yield event.plain_result(
                "⚠️ 权限不足：仅广播管理员或 AstrBot 超级管理员可用。"
            )
            return

        users = self._mentioned_users(event)
        if not users or self._has_extra_text(event):
            yield event.plain_result("用法：/broadcast_auth @某人")
            return

        admins = list(self._settings.admin_uids)
        added: list[tuple[str, str]] = []
        existed: list[tuple[str, str]] = []
        for uid, name in users:
            if uid in admins:
                existed.append((uid, name))
            else:
                admins.append(uid)
                added.append((uid, name))

        if added:
            self._settings.admin_uids = admins
            self._save_settings()
            logger.info(f"[broadcast] 新增 {len(added)} 个管理员")

        def label(uid: str, name: str) -> str:
            return f"@{name}" if name else f"@{uid}"

        lines = [
            f"✅ 已成功将 {label(uid, name)} 添加为广播管理员" for uid, name in added
        ]
        lines += [
            f"ℹ️ {label(uid, name)}：该用户已经是管理员了" for uid, name in existed
        ]
        if added:
            lines.append(f"当前管理员共 {len(self._settings.admin_uids)} 个")
        yield event.plain_result("\n".join(lines))

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

    def _is_super_admin(self, event: AstrMessageEvent) -> bool:
        """AstrBot 全局管理员（事件 role == admin）。"""
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _can_grant_admin(self, event: AstrMessageEvent) -> bool:
        """授权指令的门槛：已在广播管理员名单里，或 AstrBot 超级管理员。"""
        return self._is_admin(event) or self._is_super_admin(event)

    @staticmethod
    def _message_components(event: AstrMessageEvent) -> list[Any]:
        getter = getattr(event, "get_messages", None)
        if not callable(getter):
            return []
        try:
            return list(getter() or [])
        except Exception:
            return []

    @staticmethod
    def _component_type(comp: Any) -> str:
        if isinstance(comp, dict):
            kind = comp.get("type", "")
        else:
            kind = getattr(comp, "type", "")
        kind = getattr(kind, "value", kind)
        return str(kind or "").lower()

    def _raw_mentions(self, event: AstrMessageEvent) -> list[dict[str, str]]:
        """QQ 官方等平台把非机器人 @ 放在 raw_message.mentions 里。"""
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        mentions = getattr(raw, "mentions", None) or []
        found: list[dict[str, str]] = []
        for mention in mentions:
            if isinstance(mention, dict):
                if mention.get("is_you") is True:
                    continue
                uid = mention.get("id") or mention.get("member_openid") or ""
                name = mention.get("username") or mention.get("member_name") or ""
            else:
                if getattr(mention, "is_you", False) is True:
                    continue
                uid = (
                    getattr(mention, "id", None)
                    or getattr(mention, "member_openid", None)
                    or getattr(mention, "user_openid", None)
                    or ""
                )
                name = getattr(mention, "username", "") or getattr(
                    mention, "member_name", ""
                )
            if uid:
                found.append({"id": str(uid), "name": str(name or "")})
        return found

    @staticmethod
    def _raw_text(event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        parts = [
            str(getattr(event, "message_str", "") or ""),
            str(getattr(message_obj, "message_str", "") or ""),
            str(getattr(raw, "content", "") or ""),
        ]
        return "\n".join(part for part in parts if part)

    def _mentioned_users(self, event: AstrMessageEvent) -> list[tuple[str, str]]:
        """按「消息段 → raw.mentions → 文本标记」三路提取被 @ 的用户。"""
        found: list[tuple[str, str]] = []

        def add(uid: Any, name: Any = "") -> None:
            value = str(uid or "").strip()
            if not value or value.lower() in {"all", "everyone"}:
                return
            if any(value == item[0] for item in found):
                return
            found.append((value, str(name or "").strip()))

        for comp in self._message_components(event):
            if self._component_type(comp) != "at":
                continue
            if isinstance(comp, dict):
                data = comp.get("data") or {}
                add(data.get("qq"), data.get("name"))
            else:
                add(getattr(comp, "qq", ""), getattr(comp, "name", ""))

        for mention in self._raw_mentions(event):
            add(mention["id"], mention["name"])

        for match in _AT_MARKUP_RE.finditer(self._raw_text(event)):
            add(match.group("a") or match.group("b"))

        self_id = ""
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        return [(uid, name) for uid, name in found if uid != self_id]

    def _has_extra_text(self, event: AstrMessageEvent) -> bool:
        """指令后除 @ 之外是否还有别的文字。"""
        text = str(getattr(event, "message_str", "") or "")
        text = re.sub(r"^\s*/\S+\s*", "", text, count=1)
        text = _AT_MARKUP_RE.sub(" ", text)
        text = _AT_TEXT_RE.sub(" ", text)
        return bool(text.strip())

    @staticmethod
    def _payload(event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "")
        return re.sub(r"^\s*/\S+\s*", "", text, count=1, flags=re.DOTALL).strip()

    async def _empty_targets_hint(self) -> str:
        sessions = await self._collect_sessions()
        if not sessions:
            return (
                "⚠️ 还没有发现任何会话，广播无法发送。\n\n"
                "插件会从这些来源发现会话：\n"
                "1. 平台适配器内存中的会话缓存；\n"
                "2. AstrBot 数据库中的已知会话（与 WebUI 会话列表同源）。\n\n"
                "如果仍然为空：\n"
                "· 在目标群/私聊里和机器人说一句话，AstrBot 会自动记下会话名；\n"
                "· 或挂载 aiocqhttp 后在插件配置页点「刷新会话」。"
            )
        return (
            f"已知 {len(sessions)} 个会话，但一个都没有勾选。\n"
            "请到插件配置页的「目标会话」里勾选要广播的会话。"
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
                f"⚠️ 有 {len(result.missing_targets)} 个已勾选 UMO 未在会话列表中发现，"
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
        for task in (self._task, self._drain_task):
            if task and not task.done():
                task.cancel()
        self._save_state()


def _split_umo(umo: str) -> tuple[str, str, str]:
    parts = umo.split(":", 2)
    if len(parts) < 3:
        return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "", umo)
    return parts[0], parts[1], parts[2]
