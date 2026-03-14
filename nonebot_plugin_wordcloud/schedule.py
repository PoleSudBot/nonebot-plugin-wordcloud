from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

import nonebot_plugin_saa as saa
from nonebot.compat import model_dump
from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_cesaa import get_messages_plain_text
from nonebot_plugin_orm import get_session
from sqlalchemy import JSON, Select, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import plugin_config
from .data_source import get_wordcloud
from .model import Schedule
from .utils import (
    get_datetime_now_with_timezone,
    get_mask_key,
    get_time_with_scheduler_timezone,
    time_astimezone,
)

if TYPE_CHECKING:
    from apscheduler.job import Job

saa.enable_auto_select_bot()


class Scheduler:
    def __init__(self):
        # 默认定时任务的 key 为 default
        # 其他则为 ISO 8601 格式的时间字符串
        self.schedules: dict[str, Job] = {}

        # 转换到 APScheduler 的时区
        scheduler_time = get_time_with_scheduler_timezone(
            plugin_config.wordcloud_default_schedule_time
        )
        # 添加默认定时任务
        self.schedules["default"] = scheduler.add_job(
            self.run_task,
            "cron",
            hour=scheduler_time.hour,
            minute=scheduler_time.minute,
            second=scheduler_time.second,
        )

    async def update(self):
        """更新定时任务"""
        async with get_session() as session:
            statement = (
                select(Schedule.time)
                .group_by(Schedule.time)
                .where(Schedule.time != None)  # noqa: E711
            )
            schedule_times = await session.scalars(statement)
            for schedule_time in schedule_times:
                assert schedule_time is not None
                time_str = schedule_time.isoformat()
                if time_str not in self.schedules:
                    # 转换到 APScheduler 的时区，因为数据库中的时间是 UTC 时间
                    scheduler_time = get_time_with_scheduler_timezone(
                        schedule_time.replace(tzinfo=ZoneInfo("UTC"))
                    )
                    self.schedules[time_str] = scheduler.add_job(
                        self.run_task,
                        "cron",
                        hour=scheduler_time.hour,
                        minute=scheduler_time.minute,
                        second=scheduler_time.second,
                        args=(schedule_time,),
                    )
                    logger.debug(
                        f"已添加每日词云定时发送任务，发送时间：{time_str} UTC"
                    )

    @staticmethod
    def get_summary_ranges(dt: datetime) -> list[tuple[str, datetime, datetime]]:
        """获取当前时刻需要生成的定时总结范围"""
        day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        ranges = [("今日词云总结", day_start, dt)]

        if dt.weekday() == 6:
            week_start = day_start - timedelta(days=dt.weekday())
            ranges.append(("本周词云总结", week_start, dt))

        tomorrow = dt + timedelta(days=1)
        if tomorrow.month != dt.month:
            month_start = day_start.replace(day=1)
            ranges.append(("本月词云总结", month_start, dt))

        if tomorrow.year != dt.year:
            year_start = day_start.replace(month=1, day=1)
            ranges.append(("本年词云总结", year_start, dt))

        return ranges

    @staticmethod
    def build_summary_message(title: str, image: bytes | None) -> saa.MessageFactory:
        """构造定时总结消息"""
        if image:
            return saa.MessageFactory([saa.Text(f"{title}\n"), saa.Image(image)])
        return saa.MessageFactory(f"{title}\n没有足够的数据生成词云")

    async def run_task(self, time: Optional[time] = None):
        """执行定时任务

        时间为 UTC 时间，并且没有时区信息
        如果没有传入时间，则执行默认定时任务
        """
        async with get_session() as session:
            statement = select(Schedule).where(Schedule.time == time)
            results = await session.scalars(statement)
            schedules = results.all()
            # 如果该时间没有需要执行的定时任务，且不是默认任务则从任务列表中删除该任务
            if time and not schedules:
                self.schedules.pop(time.isoformat()).remove()
                return
            logger.info(f"开始发送词云定时总结，时间为 {time or '默认时间'}")
            for schedule in schedules:
                target = schedule.saa_target
                dt = get_datetime_now_with_timezone()
                mask_key = get_mask_key(target)
                for title, start, stop in self.get_summary_ranges(dt):
                    messages = await get_messages_plain_text(
                        target=target,
                        types=["message"],
                        time_start=start,
                        time_stop=stop,
                        exclude_user_ids=plugin_config.wordcloud_exclude_user_ids,
                    )
                    image = await get_wordcloud(messages, mask_key)
                    msg = self.build_summary_message(title, image)

                    try:
                        await msg.send_to(target)
                    except Exception:
                        logger.exception(f"{target} 发送 {title} 失败")

    async def get_schedule(self, target: saa.PlatformTarget) -> Optional[time]:
        """获取定时任务时间"""
        async with get_session() as session:
            statement = self.select_target_statement(target, session)
            results = await session.scalars(statement)
            if schedule := results.one_or_none():
                if schedule.time:
                    # 将时间转换为本地时间
                    return time_astimezone(
                        schedule.time.replace(tzinfo=ZoneInfo("UTC"))
                    )
                else:
                    return plugin_config.wordcloud_default_schedule_time

    async def add_schedule(
        self, target: saa.PlatformTarget, *, time: Optional[time] = None
    ):
        """添加定时任务

        时间需要带时区信息
        """
        # 将时间转换为 UTC 时间
        if time:
            time = time_astimezone(time, ZoneInfo("UTC"))

        async with get_session() as session:
            statement = self.select_target_statement(target, session)
            results = await session.scalars(statement)
            if schedule := results.one_or_none():
                schedule.time = time
            else:
                schedule = Schedule(time=time, target=model_dump(target))
                session.add(schedule)
            await session.commit()
        await self.update()

    async def remove_schedule(self, target: saa.PlatformTarget):
        """删除定时任务"""
        async with get_session() as session:
            statement = self.select_target_statement(target, session)
            results = await session.scalars(statement)
            if schedule := results.one_or_none():
                await session.delete(schedule)
                await session.commit()

    @staticmethod
    def select_target_statement(
        target: saa.PlatformTarget, session: AsyncSession
    ) -> Select[tuple[Schedule]]:
        """获取查询目标的语句

        MySQL 需要手动将 JSON 类型的字段转换为 JSON 类型
        """
        engine = session.get_bind()
        if engine.dialect.name == "mysql":
            return select(Schedule).where(
                Schedule.target == cast(model_dump(target), JSON)
            )
        return select(Schedule).where(Schedule.target == model_dump(target))


schedule_service = Scheduler()
