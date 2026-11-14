"""人格切换：让用户只在管理员允许的人格里切换。

设计要点：
- 可切换范围由管理员在 WebUI 插件面板勾选（写入插件配置 allowed_personas）。
- 切换写的是 AstrBot 原生的会话人格（conversation.persona_id + 会话覆盖），
  所以 WebUI 看得到、下一条消息就生效，不需要重启。
- 不依赖 Redis、数据库以外的任何第三方服务。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

try:  # AstrBot >= 4.29
    from astrbot.api.web import error_response, json_response, request

    _NEW_WEB_HELPERS = True
except Exception:  # pragma: no cover - 4.16 ~ 4.28 的 Quart 兼容路径
    from quart import jsonify, request

    _NEW_WEB_HELPERS = False

    def json_response(payload):
        return jsonify(payload)

    def error_response(message, status_code=400):
        return jsonify({"status": "error", "message": message}), status_code


async def _read_json_body() -> dict:
    """同时兼容新旧两套请求对象。"""
    if _NEW_WEB_HELPERS:
        payload = await request.json(default={})
    else:  # pragma: no cover
        payload = await request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}

from .core.logic import (
    RESERVED_DEFAULT_PERSONA,
    calc_vote_threshold,
    chunk_lines,
    effective_allowed,
    format_seconds,
    format_persona_line,
    is_agree_text,
    parse_index,
    resolve_target,
    summarize_prompt,
    unique_preserve,
)

PLUGIN_NAME = "astrbot_plugin_persona_switcher"


@dataclass
class VoteSession:
    """一次群聊人格切换投票。"""

    group_id: str
    umo: str
    target: str
    action: str
    proposer_id: str
    proposer_name: str
    threshold: int
    member_count: int
    code: str
    message_id: str | None
    created_at: float
    expires_at: float
    require_quote: bool = True
    keywords: list[str] = field(default_factory=list)
    votes: dict[str, str] = field(default_factory=dict)
    task: asyncio.Task | None = None


ACTIVE_VOTES: dict[str, VoteSession] = {}


def _reply_matches(event: AstrMessageEvent, session: VoteSession) -> bool:
    """检查消息是否引用了提案消息。"""
    self_id = str(event.get_self_id() or "")
    for component in event.get_messages():
        if not isinstance(component, Reply):
            continue
        sender_id = str(getattr(component, "sender_id", "") or "")
        if sender_id and self_id and sender_id != self_id:
            continue
        reply_id = str(getattr(component, "id", "") or "")
        if session.message_id and reply_id and reply_id == str(session.message_id):
            return True
        quoted_text = (
            f"{getattr(component, 'message_str', '') or ''} "
            f"{getattr(component, 'text', '') or ''}"
        )
        if session.code and session.code in quoted_text:
            return True
    return False


class GroupVoteFilter(filter.CustomFilter):
    """只在「群里有投票 + 消息引用了提案 + 内容是赞成」时唤醒事件。

    这样不会因为监听群消息而把每条群消息都当成唤醒，避免误触发 LLM。
    """

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        if event.is_private_chat():
            return False
        group_id = str(event.get_group_id() or "")
        session = ACTIVE_VOTES.get(group_id)
        if not session or time.time() > session.expires_at:
            return False
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return False
        if not is_agree_text(event.message_str, session.keywords):
            return False
        if session.require_quote and not _reply_matches(event, session):
            return False
        return True


class PersonaSwitcherPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self._save_lock = asyncio.Lock()
        self._votes: dict[str, VoteSession] = ACTIVE_VOTES

        context.register_web_api(
            f"/{PLUGIN_NAME}/state",
            self.api_get_state,
            ["GET"],
            "读取可切换人格设置与全部人格",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/save",
            self.api_save_state,
            ["POST"],
            "保存可切换人格设置",
        )

    # ------------------------------------------------------------------ 配置
    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _vote_cfg(self) -> dict:
        raw = self.config.get("group_vote") or {}
        return raw if isinstance(raw, dict) else {}

    def _vote_bool(self, key: str, default: bool) -> bool:
        value = self._vote_cfg().get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _vote_num(self, key: str, default: float, cast=float):
        try:
            return cast(self._vote_cfg().get(key, default))
        except (TypeError, ValueError):
            return cast(default)

    def _allowed_list(self) -> list[str]:
        raw = self.config.get("allowed_personas") or []
        if not isinstance(raw, list):
            raw = [raw]
        return unique_preserve([str(item).strip() for item in raw if str(item).strip()])

    # --------------------------------------------------------------- 数据查询
    async def _all_personas(self) -> list[dict]:
        personas = await self.context.persona_manager.get_all_personas()
        rows: list[dict] = []
        for persona in personas:
            persona_id = getattr(persona, "persona_id", None)
            if not persona_id:
                continue
            rows.append(
                {
                    "id": persona_id,
                    "name": getattr(persona, "name", None) or persona_id,
                    "preview": summarize_prompt(getattr(persona, "system_prompt", ""), 80),
                }
            )
        rows.sort(key=lambda row: (row["id"] != RESERVED_DEFAULT_PERSONA, row["id"].lower()))
        return rows

    def _allowed_ids(self, all_ids: list[str]) -> list[str]:
        return effective_allowed(
            all_ids,
            self._allowed_list(),
            self._cfg_bool("allow_all_personas", True),
        )

    async def _current_persona_id(self, umo: str) -> str | None:
        conversation_manager = self.context.conversation_manager
        try:
            cid = await conversation_manager.get_curr_conversation_id(umo)
            if cid:
                conversation = await conversation_manager.get_conversation(umo, cid)
                if conversation and conversation.persona_id:
                    return conversation.persona_id
        except Exception:
            logger.debug("[persona-switcher] 读取会话人格失败", exc_info=True)

        return self._default_persona_id(umo)

    def _default_persona_id(self, umo: str) -> str:
        try:
            conf = self.context.get_config(umo)
        except Exception:
            conf = {}
        agent = conf.get("agent", {}) if isinstance(conf, dict) else {}
        agent_config = agent.get("config", {}) if isinstance(agent, dict) else {}
        if agent.get("runtime", "astrbot") == "astrbot":
            persona = agent_config.get("persona", {}) if isinstance(agent_config, dict) else {}
            persona_id = persona.get("persona_id") if isinstance(persona, dict) else None
        else:
            persona_id = agent_config.get("persona_id") if isinstance(agent_config, dict) else None
        if not persona_id and isinstance(conf, dict):
            persona_id = conf.get("provider_settings", {}).get("default_personality")
        return persona_id or RESERVED_DEFAULT_PERSONA

    # ------------------------------------------------------------------ 切换
    async def _apply_persona(self, event: AstrMessageEvent, persona_id: str) -> None:
        umo = event.unified_msg_origin
        clear_context = self._cfg_bool("clear_context_on_switch", False)
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        await self.context.conversation_manager.update_conversation(
            unified_msg_origin=umo,
            conversation_id=cid,
            persona_id=persona_id,
            history=[] if clear_context else None,
        )
        if clear_context:
            event.set_extra("_clean_ltm_session", True)

    async def _reset_persona(self, event: AstrMessageEvent) -> None:
        umo = event.unified_msg_origin
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        # persona_id 传空串时，ConversationManager 会把会话覆盖里的
        # persona 路径置为 None，相当于恢复继承默认人格。
        await self.context.conversation_manager.update_conversation(
            unified_msg_origin=umo,
            conversation_id=cid,
            persona_id="",
        )

    def _scope_error(self, event: AstrMessageEvent) -> str | None:
        if event.is_private_chat() and not self._cfg_bool("allow_private_chat", True):
            return "本插件已关闭私聊人格切换。"
        if not event.is_private_chat() and not self._cfg_bool("allow_group_chat", True):
            return "本插件已关闭群聊人格切换。"
        if self._cfg_bool("admin_only_switch", False) and not event.is_admin():
            return "只有管理员才能切换人格。"
        return None

    async def _switch(self, event: AstrMessageEvent, target: str) -> str:
        scope_error = self._scope_error(event)
        if scope_error:
            return scope_error

        persona_id, error = await self._resolve_persona_target(event, target)
        if error:
            return error

        try:
            await self._apply_persona(event, persona_id)
        except Exception as exc:
            logger.error("[persona-switcher] 切换人格失败: %s", exc, exc_info=True)
            return f"切换失败：{exc}"

        logger.info(
            "[persona-switcher] %s 切换人格为 %s",
            event.unified_msg_origin,
            persona_id,
        )
        return f"已切换人格为「{persona_id}」，下一条消息生效。"

    async def _resolve_persona_target(
        self, event: AstrMessageEvent, target: str
    ) -> tuple[str | None, str | None]:
        """把用户输入解析成白名单内的人格 ID。"""
        all_rows = await self._all_personas()
        all_ids = [row["id"] for row in all_rows]
        allowed = self._allowed_ids(all_ids)
        if not allowed:
            return None, "管理员还没有开放任何可切换的人格。"

        by_index = parse_index(target, allowed)
        if by_index:
            return by_index, None

        resolution = resolve_target(target, allowed)
        if resolution.ambiguous:
            shown = "、".join(resolution.ambiguous[:8])
            return None, f"{resolution.error}：{shown}"
        if not resolution.persona_id:
            # 目标不在白名单里时给明确提示，而不是假装找不到
            if resolve_target(target, all_ids).persona_id:
                return None, "这个人格没有对普通用户开放，请联系管理员。"
            return None, resolution.error or "没有找到这个人格"
        return resolution.persona_id, None

    # ------------------------------------------------------------- 群聊投票
    def _group_vote_active(self, event: AstrMessageEvent) -> bool:
        if event.is_private_chat():
            return False
        if not self._vote_bool("enabled", True):
            return False
        if self._vote_bool("admin_bypass", True) and event.is_admin():
            return False
        return True

    def _vote_keywords(self) -> list[str]:
        raw = self._vote_cfg().get("keywords")
        if isinstance(raw, list) and raw:
            return [str(item).strip() for item in raw if str(item).strip()]
        return ["同意", "赞成", "支持", "+1", "yes", "agree"]

    async def _group_member_count(self, event: AstrMessageEvent) -> int:
        group_id = str(event.get_group_id() or "")
        api = getattr(getattr(event, "bot", None), "api", None)
        if not api or not group_id.isdigit():
            return 0
        try:
            ret = await api.call_action("get_group_info", group_id=int(group_id))
            data = ret.get("data") if isinstance(ret, dict) else None
            data = data if isinstance(data, dict) else (ret if isinstance(ret, dict) else {})
            count = int(data.get("member_count") or 0)
            if count > 0:
                return count
        except Exception:
            logger.debug("[persona-switcher] get_group_info 失败", exc_info=True)
        try:
            ret = await api.call_action("get_group_member_list", group_id=int(group_id))
            data = ret.get("data") if isinstance(ret, dict) else None
            if isinstance(data, list):
                return len(data)
        except Exception:
            logger.debug("[persona-switcher] get_group_member_list 失败", exc_info=True)
        return 0

    async def _send_group_text(self, event: AstrMessageEvent, text: str) -> str | None:
        """优先走 OneBot API 发群消息，好拿到 message_id 用于校验引用。"""
        group_id = str(event.get_group_id() or "")
        api = getattr(getattr(event, "bot", None), "api", None)
        if api and group_id.isdigit():
            try:
                ret = await api.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=[{"type": "text", "data": {"text": text}}],
                )
                data = ret.get("data") if isinstance(ret, dict) else None
                data = data if isinstance(data, dict) else (ret if isinstance(ret, dict) else {})
                message_id = data.get("message_id")
                if message_id is not None:
                    return str(message_id)
            except Exception:
                logger.warning(
                    "[persona-switcher] send_group_msg 失败，回退 event.send",
                    exc_info=True,
                )
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception:
            logger.error("[persona-switcher] 发送群消息失败", exc_info=True)
        return None

    async def _send_umo_text(self, umo: str, text: str) -> None:
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception:
            logger.warning("[persona-switcher] 主动发送消息失败: %s", umo, exc_info=True)

    async def _start_vote(
        self,
        event: AstrMessageEvent,
        target: str,
        *,
        action: str = "switch",
    ) -> str | None:
        """发起投票；返回需要提示给用户的错误，成功发起时返回 None。"""
        scope_error = self._scope_error(event)
        if scope_error:
            return scope_error

        group_id = str(event.get_group_id() or "")
        if not group_id:
            return "只能在群聊里发起人格切换投票。"

        if action == "reset":
            persona_id = RESERVED_DEFAULT_PERSONA
        else:
            persona_id, error = await self._resolve_persona_target(event, target)
            if error:
                return error

        existing = self._votes.get(group_id)
        if existing:
            remaining = int(existing.expires_at - time.time())
            if remaining > 0:
                return (
                    f"本群已有一个切到「{existing.target}」的投票，"
                    f"还剩 {format_seconds(remaining)}，先等它结束吧。"
                )
            self._votes.pop(group_id, None)
            if existing.task:
                existing.task.cancel()

        member_count = await self._group_member_count(event)
        ratio = max(0.0, self._vote_num("ratio", 0.02, float))
        min_votes = max(1, self._vote_num("min_votes", 3, int))
        max_votes = max(0, self._vote_num("max_votes", 20, int))
        threshold = calc_vote_threshold(
            member_count,
            ratio,
            min_votes,
            max_votes,
            max(1, self._vote_num("default_threshold", 5, int)),
        )
        timeout = max(30, self._vote_num("timeout_seconds", 300, int))
        code = "PV-" + secrets.token_hex(3).upper()
        now = time.time()
        session = VoteSession(
            group_id=group_id,
            umo=event.unified_msg_origin,
            target="默认人格" if action == "reset" else persona_id,
            action=action,
            proposer_id=str(event.get_sender_id()),
            proposer_name=event.get_sender_name() or str(event.get_sender_id()),
            threshold=threshold,
            member_count=member_count,
            code=code,
            message_id=None,
            created_at=now,
            expires_at=now + timeout,
            require_quote=self._vote_bool("require_quote", True),
            keywords=self._vote_keywords(),
        )

        count_proposer = self._vote_bool("count_proposer", True)
        if count_proposer:
            session.votes[session.proposer_id] = session.proposer_name

        base = (
            f"【人格切换投票】{code}\n"
            f"提议：把本群人格切换为「{session.target}」\n"
        )
        if member_count > 0:
            base += (
                f"通过票数：{threshold} 票"
                f"（群人数 {member_count}，按 {ratio * 100:.1f}% 计算，"
                f"下限 {min_votes}、上限 {max_votes or '不限'}）\n"
            )
        else:
            base += f"通过票数：{threshold} 票（群人数未知，按默认值）\n"
        base += (
            f"投票方式：引用本条消息并发送「{self._vote_keywords()[0]}」\n"
            f"截止：{format_seconds(timeout)}后\n"
        )
        if count_proposer:
            base += f"发起人 {session.proposer_name} 已计 1 票。"
        else:
            base += "发起人不自动计票。"

        message_id = await self._send_group_text(event, base)
        session.message_id = message_id
        self._votes[group_id] = session
        session.task = asyncio.create_task(self._vote_watchdog(session))

        logger.info(
            "[persona-switcher] 群 %s 发起投票 %s -> %s，阈值 %s/%s",
            group_id,
            code,
            session.target,
            len(session.votes),
            threshold,
        )

        if len(session.votes) >= threshold:
            await self._finish_vote(group_id, passed=True, event=event)
        return None

    async def _vote_watchdog(self, session: VoteSession) -> None:
        try:
            await asyncio.sleep(max(1.0, session.expires_at - time.time()))
            if self._votes.get(session.group_id) is not session:
                return
            self._votes.pop(session.group_id, None)
            await self._send_umo_text(
                session.umo,
                f"【人格切换投票】{session.code} 已超时"
                f"（{len(session.votes)}/{session.threshold} 票），本群人格未切换。",
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("[persona-switcher] 投票看门狗异常", exc_info=True)

    async def _finish_vote(
        self,
        group_id: str,
        *,
        passed: bool,
        event: AstrMessageEvent | None = None,
        reason: str = "",
    ) -> None:
        session = self._votes.pop(group_id, None)
        if not session:
            return
        if session.task:
            session.task.cancel()

        if not passed:
            await self._send_umo_text(
                session.umo,
                f"【人格切换投票】{session.code} {reason or '未通过'}，本群人格未切换。",
            )
            return

        if event is None:
            return
        try:
            if session.action == "reset":
                await self._reset_persona(event)
            else:
                await self._apply_persona(event, session.target)
        except Exception as exc:
            logger.error("[persona-switcher] 投票通过后切换失败: %s", exc, exc_info=True)
            await self._send_umo_text(
                session.umo,
                f"【人格切换投票】{session.code} 已通过，但切换失败：{exc}",
            )
            return

        await self._send_umo_text(
            session.umo,
            f"【人格切换投票】{session.code} 通过"
            f"（{len(session.votes)}/{session.threshold} 票），"
            f"本群人格已{'恢复默认' if session.action == 'reset' else f'切换为「{session.target}」'}。",
        )
        logger.info(
            "[persona-switcher] 群 %s 投票 %s 通过，目标 %s",
            group_id,
            session.code,
            session.target,
        )

    @filter.custom_filter(GroupVoteFilter, False)
    async def on_group_vote_message(self, event: AstrMessageEvent):
        """群里的投票消息：必须引用提案消息，且只算一人一票。"""
        group_id = str(event.get_group_id() or "")
        session = self._votes.get(group_id)
        if not session:
            return

        if time.time() > session.expires_at:
            self._votes.pop(group_id, None)
            if session.task:
                session.task.cancel()
            await self._send_umo_text(
                session.umo,
                f"【人格切换投票】{session.code} 已超时"
                f"（{len(session.votes)}/{session.threshold} 票），本群人格未切换。",
            )
            return

        if str(event.get_sender_id()) == str(event.get_self_id()):
            return
        if not is_agree_text(event.message_str, self._vote_keywords()):
            return
        if self._vote_bool("require_quote", True) and not _reply_matches(
            event, session
        ):
            return

        user_id = str(event.get_sender_id())
        if user_id in session.votes:
            return
        session.votes[user_id] = event.get_sender_name() or user_id
        event.stop_event()

        logger.info(
            "[persona-switcher] 投票 %s 收到 %s 的票，%s/%s",
            session.code,
            user_id,
            len(session.votes),
            session.threshold,
        )
        if len(session.votes) >= session.threshold:
            await self._finish_vote(group_id, passed=True, event=event)

    # --------------------------------------------------------------- 文本渲染
    async def _render_list(self, umo: str) -> str:
        all_rows = await self._all_personas()
        all_ids = [row["id"] for row in all_rows]
        allowed = self._allowed_ids(all_ids)
        if not allowed:
            return "管理员还没有开放任何可切换的人格。"

        current = await self._current_persona_id(umo)
        show_preview = self._cfg_bool("show_prompt_preview", False)
        preview_by_id = {row["id"]: row["preview"] for row in all_rows}

        lines: list[str] = ["可切换人格："]
        rendered = []
        for index, persona_id in enumerate(allowed, start=1):
            mark = "（当前）" if persona_id == current else ""
            preview = preview_by_id.get(persona_id, "") if show_preview else ""
            rendered.append(format_persona_line(index, persona_id, preview) + mark)
        shown, remaining = chunk_lines(rendered, self._cfg_int("max_list_items", 20))
        lines.extend(shown)
        if remaining:
            lines.append(f"……还有 {remaining} 个未显示。")
        lines.append("用法：/人格切换 <编号或人格ID>")
        return "\n".join(lines)

    async def _render_status(self, umo: str) -> str:
        current = await self._current_persona_id(umo)
        all_rows = await self._all_personas()
        all_ids = [row["id"] for row in all_rows]
        allowed = self._allowed_ids(all_ids)
        switchable = "可切换" if current in allowed else "不在可切换列表"
        return (
            f"当前人格：{current}（{switchable}）\n"
            "发送 /人格 查看列表，/人格切换 <编号> 切换。"
        )

    # ------------------------------------------------------------------ 指令
    @staticmethod
    def _command_args(event: AstrMessageEvent) -> str:
        """取指令名之后的完整参数（保留空格，不依赖内部 GreedyStr）。"""
        text = (event.get_message_str() or "").strip()
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @filter.command("人格", alias={"persona", "人设"})
    async def cmd_persona(self, event: AstrMessageEvent):
        """人格：查看当前人格、列表或切换。"""
        umo = event.unified_msg_origin
        action = self._command_args(event)
        lowered = action.lower()

        if lowered in {"", "帮助", "help", "?"}:
            yield event.plain_result(
                "人格切换\n"
                "/人格 查看当前与列表\n"
                "/人格切换 <编号或ID>\n"
                "/人格当前\n"
                "/人格重置\n"
                "/人格投票 查看群投票进度（/人格投票取消 取消）\n"
                "群聊切换需要投票，投票请引用提案消息。\n"
                "管理员还可用 /人格允许、/人格禁止。"
            )
            return

        if lowered in {"列表", "list", "ls"}:
            yield event.plain_result(await self._render_list(umo))
            return

        if lowered in {"当前", "current", "status"}:
            yield event.plain_result(await self._render_status(umo))
            return

        if lowered in {"重置", "reset", "默认", "default"}:
            scope_error = self._scope_error(event)
            if scope_error:
                yield event.plain_result(scope_error)
                return
            if self._group_vote_active(event):
                error = await self._start_vote(event, "", action="reset")
                if error:
                    yield event.plain_result(error)
                return
            await self._reset_persona(event)
            yield event.plain_result("已恢复默认人格。")
            return

        if lowered.startswith(("允许", "allow", "禁止", "deny")) or lowered in {
            "全部允许",
            "allow_all",
            "全部禁止",
            "deny_all",
        }:
            async for result in self._admin_update(event, action):
                yield result
            return

        if self._group_vote_active(event):
            error = await self._start_vote(event, action)
            if error:
                yield event.plain_result(error)
            return
        yield event.plain_result(await self._switch(event, action))

    @filter.command("人格列表", alias={"人设列表", "persona_list"})
    async def cmd_list(self, event: AstrMessageEvent):
        """列出可切换的人格。"""
        yield event.plain_result(await self._render_list(event.unified_msg_origin))

    @filter.command("人格当前", alias={"人设当前", "persona_current"})
    async def cmd_current(self, event: AstrMessageEvent):
        """查看当前人格。"""
        yield event.plain_result(await self._render_status(event.unified_msg_origin))

    @filter.command("人格切换", alias={"人设切换", "切人格", "persona_switch"})
    async def cmd_switch(self, event: AstrMessageEvent):
        """切换到指定人格：/人格切换 <编号或ID>。"""
        target = self._command_args(event)
        if not target:
            yield event.plain_result("用法：/人格切换 <编号或人格ID>，先发 /人格 看列表。")
            return
        if self._group_vote_active(event):
            error = await self._start_vote(event, target)
            if error:
                yield event.plain_result(error)
            return
        yield event.plain_result(await self._switch(event, target))

    @filter.command("人格重置", alias={"人设重置", "persona_reset"})
    async def cmd_reset(self, event: AstrMessageEvent):
        """恢复默认人格。"""
        scope_error = self._scope_error(event)
        if scope_error:
            yield event.plain_result(scope_error)
            return
        if self._group_vote_active(event):
            error = await self._start_vote(event, "", action="reset")
            if error:
                yield event.plain_result(error)
            return
        await self._reset_persona(event)
        yield event.plain_result("已恢复默认人格。")

    @filter.command("人格投票", alias={"投票切换", "persona_vote"})
    async def cmd_vote(self, event: AstrMessageEvent):
        """查看或取消本群的人格切换投票。"""
        group_id = str(event.get_group_id() or "")
        session = self._votes.get(group_id)
        if not session:
            yield event.plain_result(
                "本群没有进行中的切换投票。\n"
                "发起方式：/人格切换 <编号或人格ID>"
            )
            return

        action = self._command_args(event).lower()
        if action in {"取消", "cancel", "stop"}:
            if not (
                event.is_admin()
                or str(event.get_sender_id()) == session.proposer_id
            ):
                yield event.plain_result("只有发起人或管理员能取消这个投票。")
                return
            self._votes.pop(group_id, None)
            if session.task:
                session.task.cancel()
            yield event.plain_result(f"已取消投票 {session.code}。")
            return

        remaining = int(session.expires_at - time.time())
        names = "、".join(list(session.votes.values())[:10]) or "还没有人投票"
        yield event.plain_result(
            f"投票 {session.code}：切换到「{session.target}」\n"
            f"进度：{len(session.votes)}/{session.threshold} 票\n"
            f"剩余：{format_seconds(max(0, remaining))}\n"
            f"已投：{names}\n"
            f"投票请引用提案消息并发送「{self._vote_keywords()[0]}」。"
        )

    @filter.command("人格允许", alias={"人设允许", "persona_allow"})
    async def cmd_allow(self, event: AstrMessageEvent):
        """管理员：把人格加入可切换列表。/人格允许 <编号|ID|全部>"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能修改可切换人格。")
            return
        target = self._command_args(event)
        async for result in self._admin_update(event, f"允许 {target}".strip()):
            yield result

    @filter.command("人格禁止", alias={"人设禁止", "persona_deny"})
    async def cmd_deny(self, event: AstrMessageEvent):
        """管理员：把人格移出可切换列表。/人格禁止 <编号|ID|全部>"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能修改可切换人格。")
            return
        target = self._command_args(event)
        async for result in self._admin_update(event, f"禁止 {target}".strip()):
            yield result

    async def _admin_update(self, event: AstrMessageEvent, action: str):
        if not event.is_admin():
            yield event.plain_result("只有管理员能修改可切换人格。")
            return

        all_rows = await self._all_personas()
        all_ids = [row["id"] for row in all_rows]
        parts = (action or "").strip().split(maxsplit=1)
        verb = parts[0].lower()
        target = parts[1].strip() if len(parts) > 1 else ""

        is_allow = verb in {"允许", "allow"}
        if not is_allow and verb not in {"禁止", "deny"}:
            yield event.plain_result("用法：/人格允许 <编号|ID|全部> 或 /人格禁止 <编号|ID|全部>")
            return

        allow_all = self._cfg_bool("allow_all_personas", True)
        allowed = self._allowed_list()
        current_effective = self._allowed_ids(all_ids)

        if target.lower() in {"全部", "all"}:
            if is_allow:
                async with self._save_lock:
                    await self._save_settings(True, all_ids)
                yield event.plain_result("已开放全部人格。")
            else:
                async with self._save_lock:
                    await self._save_settings(False, [])
                yield event.plain_result("已禁止全部人格切换。")
            return

        if not target:
            yield event.plain_result("请指定要允许/禁止的人格编号或 ID。")
            return

        persona_id = parse_index(target, current_effective if not is_allow else all_ids)
        if not persona_id:
            resolution = resolve_target(target, all_ids)
            persona_id = resolution.persona_id
        if not persona_id:
            yield event.plain_result("没有找到这个人格。")
            return

        if is_allow:
            if allow_all:
                yield event.plain_result(
                    "当前已允许全部人格。若要只开放部分人格，请先关闭「允许使用全部人格」再勾选。"
                )
                return
            new_allowed = unique_preserve(allowed + [persona_id])
            async with self._save_lock:
                await self._save_settings(False, new_allowed)
            yield event.plain_result(f"已允许「{persona_id}」。")
        else:
            new_allowed = [pid for pid in current_effective if pid != persona_id]
            async with self._save_lock:
                await self._save_settings(False, new_allowed)
            yield event.plain_result(f"已禁止「{persona_id}」。")

    # ------------------------------------------------------------------ Web API
    async def api_get_state(self):
        """给可视化面板：全部人格 + 当前设置。"""
        try:
            rows = await self._all_personas()
        except Exception as exc:
            logger.error("[persona-switcher] 读取人格失败: %s", exc, exc_info=True)
            return error_response(f"读取人格失败：{exc}", status_code=500)

        return json_response(
            {
                "personas": rows,
                "settings": {
                    "allow_all_personas": self._cfg_bool("allow_all_personas", True),
                    "allowed_personas": self._allowed_list(),
                    "admin_only_switch": self._cfg_bool("admin_only_switch", False),
                    "clear_context_on_switch": self._cfg_bool("clear_context_on_switch", False),
                    "show_prompt_preview": self._cfg_bool("show_prompt_preview", False),
                },
            }
        )

    async def api_save_state(self):
        """保存可切换人格设置。"""
        payload = await _read_json_body()
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)

        allow_all = payload.get("allow_all_personas")
        allowed = payload.get("allowed_personas")

        if allow_all is not None and not isinstance(allow_all, bool):
            return error_response("allow_all_personas 必须是布尔值", status_code=400)
        if allowed is not None:
            if not isinstance(allowed, list) or any(not isinstance(x, str) for x in allowed):
                return error_response("allowed_personas 必须是字符串列表", status_code=400)

        async with self._save_lock:
            await self._save_settings(
                allow_all if allow_all is not None else self._cfg_bool("allow_all_personas", True),
                allowed if allowed is not None else self._allowed_list(),
            )
        return json_response({"saved": True, "settings": await self.api_get_state_value()})

    async def api_get_state_value(self) -> dict:
        rows = await self._all_personas()
        return {
            "personas": rows,
            "settings": {
                "allow_all_personas": self._cfg_bool("allow_all_personas", True),
                "allowed_personas": self._allowed_list(),
                "admin_only_switch": self._cfg_bool("admin_only_switch", False),
                "clear_context_on_switch": self._cfg_bool("clear_context_on_switch", False),
                "show_prompt_preview": self._cfg_bool("show_prompt_preview", False),
            },
        }

    async def _save_settings(self, allow_all: bool, allowed: list[str]) -> None:
        cleaned = unique_preserve([str(item).strip() for item in allowed if str(item).strip()])
        self.config["allow_all_personas"] = bool(allow_all)
        self.config["allowed_personas"] = cleaned
        await self.config.save_config_async()
        logger.info(
            "[persona-switcher] 设置已保存 allow_all=%s allowed=%s",
            allow_all,
            len(cleaned),
        )

    async def terminate(self):
        """插件卸载时取消所有投票看门狗。"""
        for session in list(self._votes.values()):
            if session.task:
                session.task.cancel()
        self._votes.clear()
