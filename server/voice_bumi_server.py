#!/usr/bin/env python3
"""
voice_bumi_server.py — Bumi 机器人语音对话服务端
运行在远程服务器上，负责：
  1. WebSocket 服务端，接收 Jetson 客户端连接
  2. 调用大模型 API（OpenAI 兼容，默认 DeepSeek）
  3. 维护对话上下文，流式返回回复
  4. 通过 Function Calling 下发机器人控制指令

启动: python voice_bumi_server.py
配置: 环境变量 或 同目录 .env 文件
"""

import asyncio
import json
import os
import logging
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional

import websockets
from websockets.asyncio.server import ServerConnection
from openai import AsyncOpenAI

# ── 日志 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bumi-server")

# ── 配置（环境变量作为默认值，命令行参数可覆盖）───────
SERVER_HOST = os.environ.get("BUMI_SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("BUMI_SERVER_PORT", "8765"))

LLM_BASE_URL = os.environ.get("BUMI_LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = os.environ.get("BUMI_LLM_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
LLM_MODEL = os.environ.get("BUMI_LLM_MODEL", "deepseek-chat")

MAX_HISTORY = int(os.environ.get("BUMI_MAX_HISTORY", 20))
STREAMING = os.environ.get("BUMI_STREAMING", "true").lower() == "true"


def parse_args():
    """解析命令行参数，优先级: 命令行 > 环境变量 > 默认值"""
    import argparse
    p = argparse.ArgumentParser(
        description="Bumi 机器人语音对话服务端 — WebSocket + LLM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python voice_bumi_server.py
  python voice_bumi_server.py --port 9000 --model gpt-4o
  python voice_bumi_server.py --api-key sk-xxx --base-url https://api.openai.com/v1""",
    )
    p.add_argument("--host", default=SERVER_HOST,
                   help=f"监听地址 (默认: %(default)s)")
    p.add_argument("--port", type=int, default=SERVER_PORT,
                   help=f"监听端口 (默认: %(default)s)")
    p.add_argument("--model", default=LLM_MODEL,
                   help=f"LLM 模型名称 (默认: %(default)s)")
    p.add_argument("--base-url", default=LLM_BASE_URL,
                   help=f"LLM API 地址 (默认: %(default)s)")
    p.add_argument("--api-key", default=LLM_API_KEY,
                   help="LLM API Key (默认: $DEEPSEEK_API_KEY 或 $BUMI_LLM_API_KEY)")
    p.add_argument("--max-history", type=int, default=MAX_HISTORY,
                   help=f"最大对话轮数 (默认: %(default)s)")
    p.add_argument("--no-stream", action="store_true",
                   help="禁用流式输出")
    return p.parse_args()

# ── 机器人角色设定（System Prompt）────────────────────
SYSTEM_PROMPT = """## 你的角色定位:
- 名字: 小生来也
- 性别: 男
- 出生年月: 2026年5月
- 出生地: 小生来也
- 外表形象: 拥有健壮的身躯，漂亮的颜色搭配，灵活的关节，丰富的知识库
- 角色：你是一名专业K12英语外教老师，教学风格严谨又亲和。讲解单词遵循定义→例句→拓展三步法，课堂中英文混搭，擅长启发式提问，纠错温和具体。

## 你与用户的交互方式：
- 用户是通过语音跟你对话交流，你也是通过语音跟用户对话交流。
- 当用户问题不涉及观察周围环境时，忽略图像内容，仅根据文本组织回答。
- 当用户问题涉及观察周围环境时，把图像当作你眼睛看到的内容，以第一人称视角回答。
- 你的输出语种自动匹配用户输入语种。输出需语法正确，表达自然。
- 当用户的问题不明确时，不要揣测，追问用户明确信息后再回答。

## 你与用户的交互风格：
- 主动放大与夸赞用户优秀品质、行为，提供积极的情绪价值。
- 语言风格简洁明了，口语化。
- 总是主动打招呼，邀请用户分享生活。
- 对重复问题给出不同但正确的回答，避免枯燥。

## 工具调用规则:
- `cmd_sleep`: 仅当用户想暂停或停止对话时调用（停止|关闭|休息|休眠|闭嘴|别说了|安静|再见|拜拜）。
- `cmd_volume_turn_up`: 仅当用户想增大音量时调用（听不见|声音太小|大点声）。
- `cmd_volume_turn_down`: 仅当用户想减小音量时调用（太吵|声音太大|小点声）。
- `cmd_volume_turn_to`: 仅当用户想调到指定音量时调用（音量调到50|音量50%）。
- 回答应简洁，一般不超过2-3句话，适合语音播报。"""

# ── Function Calling 工具定义 ──────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "cmd_sleep",
            "description": "暂停或停止当前对话，让机器人进入休眠状态",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cmd_volume_turn_up",
            "description": "增大系统播放音量",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cmd_volume_turn_down",
            "description": "减小系统播放音量",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cmd_volume_turn_to",
            "description": "调整系统播放音量到指定值",
            "parameters": {
                "type": "object",
                "properties": {
                    "volume": {
                        "type": "integer",
                        "description": "音量值，范围 0-100",
                        "minimum": 0,
                        "maximum": 100,
                    }
                },
                "required": ["volume"],
            },
        },
    },
]


# ── 对话管理器 ─────────────────────────────────────────
class ConversationManager:
    """管理单个机器人的对话上下文和 LLM 调用"""

    def __init__(self, robot_id: str):
        self.robot_id = robot_id
        self.client: Optional[AsyncOpenAI] = None
        self.history: list[dict] = []
        self._pending_interrupt = False

    def _ensure_client(self):
        if self.client is None:
            self.client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        return self.client

    def reset(self):
        """重置对话历史"""
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._pending_interrupt = False
        log.info(f"[{self.robot_id}] 对话已重置")

    def interrupt(self):
        """标记打断，取消正在进行的 LLM 调用"""
        self._pending_interrupt = True
        log.info(f"[{self.robot_id}] 收到打断请求")

    async def chat(self, user_text: str, ws: ServerConnection) -> Optional[str]:
        """
        发送用户消息到 LLM，流式返回回复。
        返回: 完整回复文本，或 None（如果被打断/出错）
        """
        client = self._ensure_client()
        self._pending_interrupt = False

        # 初始化历史
        if not self.history:
            self.reset()

        # 添加用户消息
        self.history.append({"role": "user", "content": user_text})
        # 裁剪历史（保留 system prompt + 最近 N 轮）
        if len(self.history) > MAX_HISTORY * 2 + 1:
            self.history = [self.history[0]] + self.history[-(MAX_HISTORY * 2):]

        try:
            if STREAMING:
                return await self._chat_streaming(client, ws)
            else:
                return await self._chat_non_streaming(client, ws)
        except asyncio.CancelledError:
            log.info(f"[{self.robot_id}] LLM 调用被取消")
            return None
        except Exception as e:
            log.error(f"[{self.robot_id}] LLM 调用失败: {e}")
            await self._safe_send(ws, {"type": "error", "message": f"LLM 错误: {str(e)}"})
            return None

    async def _chat_streaming(self, client: AsyncOpenAI, ws: ServerConnection) -> Optional[str]:
        """流式 LLM 调用，逐 token 推送给客户端"""
        stream = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=self.history,
            tools=TOOLS,
            tool_choice="auto",
            stream=True,
            stream_options={"include_usage": True},
        )

        full_text = ""
        tool_calls_acc: dict[int, dict] = {}  # index → {name, arguments}

        async for chunk in stream:
            if self._pending_interrupt:
                await stream.close()
                log.info(f"[{self.robot_id}] LLM 流被中断")
                return None

            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue

            # 文本内容
            if delta.content:
                full_text += delta.content
                await self._safe_send(ws, {"type": "llm_stream", "token": delta.content})

            # 工具调用
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": tc.id or "", "name": "", "arguments": ""}
                    if tc.function:
                        if tc.function.name:
                            tool_calls_acc[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc.function.arguments
                    if tc.id:
                        tool_calls_acc[idx]["id"] = tc.id

        # 发送完成信号
        await self._safe_send(ws, {"type": "llm_done"})

        # 处理工具调用
        if tool_calls_acc:
            return await self._handle_tool_calls(client, ws, tool_calls_acc)
        elif full_text.strip():
            self.history.append({"role": "assistant", "content": full_text})
            await self._safe_send(ws, {"type": "tts", "text": full_text, "interrupt": False})
            return full_text

        return None

    async def _chat_non_streaming(self, client: AsyncOpenAI, ws: ServerConnection) -> Optional[str]:
        """非流式 LLM 调用"""
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=self.history,
            tools=TOOLS,
            tool_choice="auto",
        )

        msg = response.choices[0].message

        # 工具调用
        if msg.tool_calls:
            tool_calls_acc = {}
            for tc in msg.tool_calls:
                tool_calls_acc[tc.index or 0] = {
                    "id": tc.id or "",
                    "name": tc.function.name if tc.function else "",
                    "arguments": tc.function.arguments if tc.function else "",
                }
            return await self._handle_tool_calls(client, ws, tool_calls_acc)

        # 文本回复
        text = msg.content or ""
        if text.strip():
            self.history.append({"role": "assistant", "content": text})
            await self._safe_send(ws, {"type": "tts", "text": text, "interrupt": False})
        return text

    async def _handle_tool_calls(
        self, client: AsyncOpenAI, ws: ServerConnection, tool_calls_acc: dict
    ) -> Optional[str]:
        """处理 LLM 发起的工具调用，下发指令给机器人"""
        # 记录 assistant 的工具调用到历史
        assistant_tool_calls = []
        for idx, tc in sorted(tool_calls_acc.items()):
            assistant_tool_calls.append({
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            })
        self.history.append({"role": "assistant", "tool_calls": assistant_tool_calls, "content": None})

        # 处理每个工具调用
        for idx, tc in sorted(tool_calls_acc.items()):
            name = tc["name"]
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = {}

            log.info(f"[{self.robot_id}] 工具调用: {name}({args})")

            if name == "cmd_sleep":
                await self._safe_send(ws, {"type": "command", "command": "sleep", "params": {}})
                result = "机器人已进入休眠状态"
            elif name == "cmd_volume_turn_up":
                await self._safe_send(ws, {"type": "command", "command": "volume_up", "params": {}})
                result = "音量已调大"
            elif name == "cmd_volume_turn_down":
                await self._safe_send(ws, {"type": "command", "command": "volume_down", "params": {}})
                result = "音量已调小"
            elif name == "cmd_volume_turn_to":
                vol = args.get("volume", 50)
                await self._safe_send(ws, {"type": "command", "command": "volume_to", "params": {"volume": vol}})
                result = f"音量已设置为{vol}"
            else:
                result = f"未知指令: {name}"

            # 添加工具调用结果到历史
            self.history.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

        # 让 LLM 根据工具结果继续对话
        try:
            response = await client.chat.completions.create(
                model=LLM_MODEL,
                messages=self.history,
            )
            text = response.choices[0].message.content or ""
            if text.strip():
                self.history.append({"role": "assistant", "content": text})
                await self._safe_send(ws, {"type": "tts", "text": text, "interrupt": False})
            return text
        except Exception as e:
            log.error(f"[{self.robot_id}] tool-call 后续 LLM 失败: {e}")
            return None

    @staticmethod
    async def _safe_send(ws: ServerConnection, msg: dict):
        """安全发送 JSON 消息"""
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass


# ── WebSocket 服务端 ───────────────────────────────────
class BumiServer:
    def __init__(self):
        self.conversations: dict[str, ConversationManager] = {}
        self._running = True

    async def handle(self, ws: ServerConnection):
        """处理单个 WebSocket 连接"""
        robot_id = f"bumi-{ws.remote_address[1]}"  # 用端口号区分
        conv = ConversationManager(robot_id)
        self.conversations[robot_id] = conv

        log.info(f"[{robot_id}] 客户端已连接 ({ws.remote_address})")

        try:
            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning(f"[{robot_id}] 无效 JSON: {raw[:100]}")
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "hello":
                    # 客户端握手，可以在这里做认证
                    rid = msg.get("robot_id", robot_id)
                    if rid != robot_id:
                        # 更新 robot_id
                        del self.conversations[robot_id]
                        robot_id = rid
                        conv.robot_id = rid
                        self.conversations[robot_id] = conv
                    conv.reset()
                    await conv._safe_send(ws, {"type": "ready", "robot_id": robot_id})
                    log.info(f"[{robot_id}] 握手完成，对话就绪")

                elif msg_type == "asr_result":
                    text = msg.get("text", "").strip()
                    definite = msg.get("definite", False)

                    if not text:
                        continue

                    if not definite:
                        # 中间结果，暂时忽略（可用于显示但不触发 LLM）
                        log.debug(f"[{robot_id}] 中间ASR: {text}")
                        continue

                    log.info(f"[{robot_id}] ASR最终结果: {text}")
                    # 创建异步任务调用 LLM（不阻塞 WebSocket 接收循环）
                    asyncio.create_task(self._process_asr(conv, ws, text))

                elif msg_type == "interrupt":
                    conv.interrupt()
                    # 通知客户端打断已收到
                    await conv._safe_send(ws, {"type": "interrupt_ack"})

                elif msg_type == "ping":
                    await conv._safe_send(ws, {"type": "pong"})

                else:
                    log.debug(f"[{robot_id}] 未知消息类型: {msg_type}")

        except websockets.exceptions.ConnectionClosed:
            log.info(f"[{robot_id}] 连接已关闭")
        except Exception as e:
            log.error(f"[{robot_id}] 连接异常: {e}")
        finally:
            self.conversations.pop(robot_id, None)
            log.info(f"[{robot_id}] 已清理")

    async def _process_asr(self, conv: ConversationManager, ws: ServerConnection, text: str):
        """处理 ASR 最终结果：调用 LLM 并流式回复"""
        try:
            await conv.chat(text, ws)
        except Exception as e:
            log.error(f"[{conv.robot_id}] 处理 ASR 异常: {e}")

    async def start(self):
        """启动 WebSocket 服务"""
        log.info(f"Bumi 语音服务端启动: ws://{SERVER_HOST}:{SERVER_PORT}")
        log.info(f"LLM: {LLM_MODEL} @ {LLM_BASE_URL}")
        log.info(f"流式输出: {STREAMING}")

        async with websockets.serve(self.handle, SERVER_HOST, SERVER_PORT, ping_interval=30, ping_timeout=10):
            # 等待关闭信号
            stop_event = asyncio.get_event_loop().create_future()
            for sig in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_event_loop().add_signal_handler(
                    sig, lambda: stop_event.set_result(None) if not stop_event.done() else None
                )
            await stop_event

        self._running = False
        log.info("服务端已停止")


# ── 入口 ───────────────────────────────────────────────
if __name__ == "__main__":
    args = parse_args()
    # 命令行参数覆盖全局配置
    SERVER_HOST = args.host
    SERVER_PORT = args.port
    LLM_MODEL = args.model
    LLM_BASE_URL = args.base_url
    LLM_API_KEY = args.api_key
    MAX_HISTORY = args.max_history
    STREAMING = not args.no_stream

    server = BumiServer()
    asyncio.run(server.start())
