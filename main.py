"""人格切换：让用户只在管理员允许的人格里切换。

设计要点：
- 可切换范围由管理员在 WebUI 插件面板勾选（写入插件配置 allowed_personas）。
- 切换写的是 AstrBot 原生的会话人格（conversation.persona_id + 会话覆盖），
  所以 WebUI 看得到、下一条消息就生效，不需要重启。
- 不依赖 Redis、数据库以外的任何第三方服务。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.session_overrides import get_overrides, update_overrides

from .core.logic import (
    RESERVED_DEFAULT_PERSONA,
    chunk_lines,
    effective_allowed,
    format_persona_line,
    parse_index,
    resolve_target,
    summarize_prompt,
    unique_preserve,
)

PLUGIN_NAME = "astrbot_plugin_persona_switcher"
PERSONA_OVERRIDE_KEYS = (
    "agent.config.persona.persona_id",
    "agent.config.persona_id",
)

try:  # 4.x 内部类型；老版本没有就退回普通字符串
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover - 仅兼容旧版本

    class GreedyStr(str):
        pass


class PersonaSwitcherPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self._save_lock = asyncio.Lock()

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

        try:
            overrides = await get_overrides(umo)
        except Exception:
            overrides = {}
        for key in PERSONA_OVERRIDE_KEYS:
            if overrides.get(key):
                return overrides[key]

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
        await update_overrides(umo, {key: None for key in PERSONA_OVERRIDE_KEYS})
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        if cid:
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

        all_rows = await self._all_personas()
        all_ids = [row["id"] for row in all_rows]
        allowed = self._allowed_ids(all_ids)
        if not allowed:
            return "管理员还没有开放任何可切换的人格。"

        by_index = parse_index(target, allowed)
        if by_index:
            persona_id = by_index
        else:
            resolution = resolve_target(target, allowed)
            if resolution.ambiguous:
                shown = "、".join(resolution.ambiguous[:8])
                return f"{resolution.error}：{shown}"
            if not resolution.persona_id:
                # 目标不在白名单里时给明确提示，而不是假装找不到
                if resolve_target(target, all_ids).persona_id:
                    return "这个人格没有对普通用户开放，请联系管理员。"
                return resolution.error or "没有找到这个人格"
            persona_id = resolution.persona_id

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
    @filter.command("人格", alias={"persona", "人设"})
    async def cmd_persona(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """人格：查看当前人格、列表或切换。"""
        umo = event.unified_msg_origin
        action = (args or "").strip()
        lowered = action.lower()

        if lowered in {"", "帮助", "help", "?"}:
            yield event.plain_result(
                "人格切换\n"
                "/人格 查看当前与列表\n"
                "/人格切换 <编号或ID>\n"
                "/人格当前\n"
                "/人格重置\n"
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
    async def cmd_switch(self, event: AstrMessageEvent, target: GreedyStr = ""):
        """切换到指定人格：/人格切换 <编号或ID>。"""
        if not (target or "").strip():
            yield event.plain_result("用法：/人格切换 <编号或人格ID>，先发 /人格 看列表。")
            return
        yield event.plain_result(await self._switch(event, target))

    @filter.command("人格重置", alias={"人设重置", "persona_reset"})
    async def cmd_reset(self, event: AstrMessageEvent):
        """恢复默认人格。"""
        scope_error = self._scope_error(event)
        if scope_error:
            yield event.plain_result(scope_error)
            return
        await self._reset_persona(event)
        yield event.plain_result("已恢复默认人格。")

    @filter.command("人格允许", alias={"人设允许", "persona_allow"})
    async def cmd_allow(self, event: AstrMessageEvent, target: GreedyStr = ""):
        """管理员：把人格加入可切换列表。/人格允许 <编号|ID|全部>"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能修改可切换人格。")
            return
        async for result in self._admin_update(event, f"允许 {target}".strip()):
            yield result

    @filter.command("人格禁止", alias={"人设禁止", "persona_deny"})
    async def cmd_deny(self, event: AstrMessageEvent, target: GreedyStr = ""):
        """管理员：把人格移出可切换列表。/人格禁止 <编号|ID|全部>"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能修改可切换人格。")
            return
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
        payload = await request.json(default={})
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
        """插件卸载时的清理（无持久后台任务）。"""
        return None
