"""Image Caption Fallback Proxy Plugin — main entry point.

Starts a local OpenAI-compatible HTTP proxy that routes vision requests with
primary → fallback chain logic. Registers a /image_proxy command for status
checks, hot-swap, and subcommands.
"""

from __future__ import annotations

import json
import shlex
import urllib.request

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter

from .proxy_server import ImageCaptionProxy

# Module-level singleton so the proxy survives plugin instance recreation.
_proxy: ImageCaptionProxy | None = None


def _parse_fallback_chain(config: AstrBotConfig) -> list[dict]:
    """Parse fallback_chain JSON, with backwards compat for old flat fields."""
    chain_str = str(config.get("fallback_chain", "")).strip()
    if chain_str:
        try:
            chain = json.loads(chain_str)
            if isinstance(chain, list) and chain:
                return chain
        except (json.JSONDecodeError, TypeError):
            logger.warning("fallback_chain JSON 解析失败，尝试使用旧配置格式")

    fb_api_base = str(config.get("fallback_api_base", ""))
    fb_model = str(config.get("fallback_model", ""))
    if fb_api_base and fb_model:
        return [
            {
                "api_base": fb_api_base,
                "api_key": str(config.get("fallback_api_key", "")),
                "model": fb_model,
                "timeout": int(config.get("request_timeout", 60)),
            }
        ]
    return []


def _build_primary(config: AstrBotConfig) -> dict:
    return {
        "api_base": str(config.get("primary_api_base", "")),
        "api_key": str(config.get("primary_api_key", "")),
        "model": str(config.get("primary_model", "")),
        "timeout": int(config.get("primary_timeout", 5)),
    }


def _get_or_create_proxy(config: AstrBotConfig) -> ImageCaptionProxy | None:
    """Return the running proxy, or start one if not yet running."""
    global _proxy

    host = str(config.get("proxy_host", "127.0.0.1"))
    port = int(config.get("proxy_port", 11435))

    if _proxy is not None and _proxy._server is not None:
        return _proxy

    try:
        req = urllib.request.Request(f"http://{host}:{port}/health")
        with urllib.request.urlopen(req, timeout=2):
            logger.info(f"Image caption proxy already running on {host}:{port}")
            return None
    except Exception:
        pass

    primary = _build_primary(config)
    fallbacks = _parse_fallback_chain(config)

    if not primary["api_base"] or not primary["model"]:
        logger.error("Primary image caption model not configured")
        return None

    _proxy = ImageCaptionProxy(
        host=host, port=port, primary=primary, fallbacks=fallbacks, logger=logger,
    )

    if _proxy.start():
        return _proxy

    logger.error(f"Failed to start image caption proxy on {host}:{port}")
    _proxy = None
    return None


class Main(star.Star):
    """图文转述回退代理插件"""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        _get_or_create_proxy(config)

    # ==================== /image_proxy command ====================

    @filter.command("image_proxy")
    async def cmd_image_proxy(self, event: AstrMessageEvent) -> None:
        """/image_proxy [子命令]

        无参数        查看状态和统计
        status        同上
        config        显示 AstrBot 配置片段
        stats reset   重置调用统计
        primary <api_base> <api_key> <model> [timeout]   热切换主模型
        fallback add <api_base> <api_key> <model> <timeout>  添加备用模型
        fallback del <序号>      删除指定备用模型
        fallback clear           清空所有备用模型
        fallback list            列出备用模型链
        """
        raw = event.message_str.strip()
        # Remove the command prefix (AstrBot strips the leading /)
        if raw.startswith("image_proxy"):
            raw = raw[len("image_proxy"):].strip()
        elif raw.startswith("/image_proxy"):
            raw = raw[len("/image_proxy"):].strip()

        if not raw or raw in ("status",):
            await self._cmd_status(event)
        elif raw in ("help", "帮助"):
            await self._cmd_unknown(event, "")
        elif raw == "config":
            await self._cmd_config(event)
        elif raw.startswith("stats"):
            await self._cmd_stats(event, raw)
        elif raw.startswith("primary"):
            await self._cmd_primary(event, raw)
        elif raw.startswith("fallback"):
            await self._cmd_fallback(event, raw)
        else:
            await self._cmd_unknown(event, raw)

    # ---- subcommand handlers ----

    async def _cmd_status(self, event: AstrMessageEvent) -> None:
        host = str(self.config.get("proxy_host", "127.0.0.1"))
        port = int(self.config.get("proxy_port", 11435))
        primary = _build_primary(self.config) if _proxy is None else _proxy.primary
        fallbacks = _parse_fallback_chain(self.config) if _proxy is None else _proxy.fallbacks

        proxy_alive = self._check_health(host, port)
        stats = self._fetch_stats(host, port)

        pri_model = primary.get("model", "")
        pri_timeout = primary.get("timeout", 5)

        lines = [
            "图文转述回退代理",
            "=" * 30,
            f"地址:    http://{host}:{port}/v1",
            f"状态:    {'运行中' if proxy_alive else '已停止'}",
        ]

        if _proxy is not None:
            lines.append("(配置为内存热值，可能不同于磁盘配置)")

        lines.extend([
            "",
            f"主模型:  {pri_model}  (超时 {pri_timeout}s)",
        ])
        if fallbacks:
            for i, fb in enumerate(fallbacks, 1):
                lines.append(
                    f"备用 {i}:  {fb.get('model', '?')}  (超时 {fb.get('timeout', 60)}s)"
                )
        else:
            lines.append("备用:    (未配置)")

        if stats:
            lines.append("")
            lines.append("--- 调用统计 ---")
            all_models = [pri_model] + [fb.get("model", "") for fb in fallbacks]
            for model, counts in stats.items():
                total = counts.get("success", 0) + counts.get("fail", 0)
                success = counts.get("success", 0)
                fail = counts.get("fail", 0)
                rate = (success / total * 100) if total > 0 else 0
                tag = _model_tag(model, primary, fallbacks)
                lines.append(
                    f"  {model}{tag}: {success} 成功 / {fail} 失败 (成功率 {rate:.0f}%)"
                )

            primary_fails = stats.get(pri_model, {}).get("fail", 0)
            fb_total = sum(
                stats.get(fb.get("model", ""), {}).get("success", 0)
                for fb in fallbacks
            )
            if primary_fails > 0 and fb_total > 0:
                lines.append(
                    f"\n  主模型失败 {primary_fails} 次, 备用接管 {fb_total} 次 — 回退生效"
                )
            elif primary_fails > 0:
                lines.append(f"\n  主模型失败 {primary_fails} 次，备用尚无成功记录")
        else:
            lines.append("")
            lines.append("(启动后尚未收到识图请求)")

        lines.extend([
            "",
            "子命令: status | config | stats reset | primary | fallback add/del/clear/list",
            "详情: /image_proxy help",
        ])

        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    async def _cmd_config(self, event: AstrMessageEvent) -> None:
        host = str(self.config.get("proxy_host", "127.0.0.1"))
        port = int(self.config.get("proxy_port", 11435))
        lines = [
            "--- AstrBot 配置片段 ---",
            "",
            "provider_sources 添加:",
            "  {",
            f'    "provider": "image_proxy",',
            '    "type": "openai_chat_completion",',
            '    "key": ["proxy"],',
            f'    "api_base": "http://{host}:{port}/v1",',
            '    "id": "image_proxy"',
            "  }",
            "",
            "provider 添加:",
            "  {",
            '    "id": "image_proxy/vision",',
            '    "provider_source_id": "image_proxy",',
            '    "model": "vision",',
            '    "modalities": ["text", "image"]',
            "  }",
            "",
            "default_image_caption_provider_id:",
            '  "image_proxy/vision"',
        ]
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    async def _cmd_stats(self, event: AstrMessageEvent, raw: str) -> None:
        host = str(self.config.get("proxy_host", "127.0.0.1"))
        port = int(self.config.get("proxy_port", 11435))
        args = shlex.split(raw)[1:]

        if args and args[0] == "reset":
            result = self._admin_post(host, port, "/admin/stats/reset", {})
            if result:
                event.set_result(
                    MessageEventResult().message(
                        f"已重置 {result.get('cleared', 0)} 个模型的调用统计"
                    ).use_t2i(False)
                )
            else:
                event.set_result(
                    MessageEventResult().message("代理未运行，无法操作").use_t2i(False)
                )
        else:
            await self._cmd_status(event)

    async def _cmd_primary(self, event: AstrMessageEvent, raw: str) -> None:
        host = str(self.config.get("proxy_host", "127.0.0.1"))
        port = int(self.config.get("proxy_port", 11435))

        try:
            args = shlex.split(raw)[1:]
        except ValueError:
            args = raw.split()[1:]

        if len(args) < 3:
            event.set_result(
                MessageEventResult().message(
                    "用法: /image_proxy primary <api_base> <api_key> <model> [timeout]\n"
                    "示例: /image_proxy primary http://localhost:1234/v1 lm-studio qwen-vl 5"
                ).use_t2i(False)
            )
            return

        body = {
            "api_base": args[0],
            "api_key": args[1],
            "model": args[2],
            "timeout": int(args[3]) if len(args) >= 4 else 5,
        }
        result = self._admin_post(host, port, "/admin/primary", body)
        if result and result.get("ok"):
            event.set_result(
                MessageEventResult().message(
                    f"主模型已热切换为: {result.get('model')}\n"
                    "变更立即生效（仅本次会话）。"
                ).use_t2i(False)
            )
        else:
            event.set_result(
                MessageEventResult().message("代理未运行，无法热切换").use_t2i(False)
            )

    async def _cmd_fallback(self, event: AstrMessageEvent, raw: str) -> None:
        host = str(self.config.get("proxy_host", "127.0.0.1"))
        port = int(self.config.get("proxy_port", 11435))

        try:
            args = shlex.split(raw)[1:]
        except ValueError:
            args = raw.split()[1:]

        if not args:
            await self._cmd_status(event)
            return

        sub = args[0]

        if sub == "list":
            await self._cmd_fallback_list(event)
        elif sub == "clear":
            result = self._admin_post(host, port, "/admin/fallback/clear", {})
            if result and result.get("ok"):
                event.set_result(
                    MessageEventResult().message(
                        f"已清空 {result.get('cleared', 0)} 个备用模型"
                    ).use_t2i(False)
                )
            else:
                event.set_result(
                    MessageEventResult().message("代理未运行，无法操作").use_t2i(False)
                )
        elif sub == "del":
            await self._cmd_fallback_del(event, args, host, port)
        elif sub == "add":
            await self._cmd_fallback_add(event, args, host, port)
        else:
            event.set_result(
                MessageEventResult().message(
                    f"未知 fallback 子命令: {sub}\n"
                    "可用: add / del / clear / list"
                ).use_t2i(False)
            )

    async def _cmd_fallback_list(self, event: AstrMessageEvent) -> None:
        if _proxy is not None:
            fallbacks = _proxy.fallbacks
        else:
            fallbacks = _parse_fallback_chain(self.config)

        if not fallbacks:
            event.set_result(
                MessageEventResult().message("备用模型链为空").use_t2i(False)
            )
            return

        lines = ["备用模型链:"]
        for i, fb in enumerate(fallbacks):
            lines.append(
                f"  [{i}] {fb.get('model', '?')}  "
                f"({fb.get('api_base', '?')})  超时 {fb.get('timeout', 60)}s"
            )
        lines.append("")
        lines.append("删除用: /image_proxy fallback del <序号>")
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    async def _cmd_fallback_del(
        self, event: AstrMessageEvent, args: list, host: str, port: int
    ) -> None:
        if len(args) < 2:
            event.set_result(
                MessageEventResult().message(
                    "用法: /image_proxy fallback del <序号>\n"
                    "先用 /image_proxy fallback list 查看序号"
                ).use_t2i(False)
            )
            return
        try:
            idx = int(args[1])
        except ValueError:
            event.set_result(
                MessageEventResult().message(f"无效序号: {args[1]}").use_t2i(False)
            )
            return

        result = self._admin_post(host, port, "/admin/fallback/del", {"index": idx})
        if result is None:
            event.set_result(
                MessageEventResult().message("代理未运行，无法操作").use_t2i(False)
            )
        elif result.get("ok"):
            event.set_result(
                MessageEventResult().message(
                    f"已删除备用 [{idx}]: {result.get('removed', '?')}"
                ).use_t2i(False)
            )
        else:
            event.set_result(
                MessageEventResult().message(
                    f"序号 {idx} 无效或不存在"
                ).use_t2i(False)
            )

    async def _cmd_fallback_add(
        self, event: AstrMessageEvent, args: list, host: str, port: int
    ) -> None:
        payload = args[1:]
        if len(payload) < 4:
            event.set_result(
                MessageEventResult().message(
                    "用法: /image_proxy fallback add <api_base> <api_key> <model> <timeout>\n"
                    "示例: /image_proxy fallback add https://api.siliconflow.cn/v1 sk-xxx Qwen3-VL 60"
                ).use_t2i(False)
            )
            return

        try:
            timeout = int(payload[3])
        except ValueError:
            event.set_result(
                MessageEventResult().message(f"无效超时值: {payload[3]}").use_t2i(False)
            )
            return

        body = {
            "api_base": payload[0],
            "api_key": payload[1],
            "model": payload[2],
            "timeout": timeout,
        }
        result = self._admin_post(host, port, "/admin/fallback/add", body)
        if result and result.get("ok"):
            event.set_result(
                MessageEventResult().message(
                    f"已添加备用 [{result.get('index')}]: {payload[2]}  "
                    f"超时 {timeout}s  (共 {result.get('total')} 个)"
                ).use_t2i(False)
            )
        else:
            event.set_result(
                MessageEventResult().message("代理未运行，无法添加").use_t2i(False)
            )

    async def _cmd_unknown(self, event: AstrMessageEvent, raw: str) -> None:
        event.set_result(
            MessageEventResult().message(
                f"未知子命令: {raw}\n\n"
                "可用指令:\n"
                "  /image_proxy             查看状态\n"
                "  /image_proxy config      显示配置片段\n"
                "  /image_proxy stats reset 重置统计\n"
                "  /image_proxy primary <base> <key> <model> [timeout]  热切换主模型\n"
                "  /image_proxy fallback add <base> <key> <model> <timeout>  添加备用\n"
                "  /image_proxy fallback del <序号>     删除备用\n"
                "  /image_proxy fallback clear          清空备用链\n"
                "  /image_proxy fallback list           列出备用链"
            ).use_t2i(False)
        )

    # ---- helpers ----

    @staticmethod
    def _check_health(host: str, port: int) -> bool:
        try:
            req = urllib.request.Request(f"http://{host}:{port}/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                return json.loads(resp.read()).get("status") == "ok"
        except Exception:
            return False

    @staticmethod
    def _fetch_stats(host: str, port: int) -> dict:
        try:
            req = urllib.request.Request(f"http://{host}:{port}/stats")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read())
        except Exception:
            return {}

    @staticmethod
    def _admin_post(host: str, port: int, path: str, body: dict) -> dict | None:
        """POST JSON to an admin endpoint. Returns parsed response or None."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://{host}:{port}{path}", data=data, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read())
        except Exception:
            return None


def _model_tag(model: str, primary: dict, fallbacks: list[dict]) -> str:
    """Return a tag like ' [主]' or ' [备2]' for a model name."""
    if model == primary.get("model", ""):
        return " [主]"
    for i, fb in enumerate(fallbacks, 1):
        if model == fb.get("model", ""):
            return f" [备{i}]"
    return ""
