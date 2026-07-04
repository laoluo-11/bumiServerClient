#!/usr/bin/env python3
"""
voice_bumi_client.py — Bumi 机器人语音对话客户端
运行在 Jetson 算力板 (aarch64) 上，负责：
  1. DDS 通信（与主控板 192.168.55.102 交互）— 通过 pyVoiceCtrl
  2. WebSocket 客户端（连接远程 LLM 服务端）
  3. 桥梁：将主控板的 ASR 字幕 → 转发给服务端 → 接收 LLM 回复 → DDS TTS 播报

前置条件:
  - pyVoiceCtrl.cpython-312-aarch64-linux-gnu.so 在同目录
  - dds.xml 在同目录
  - LD_LIBRARY_PATH 包含 ddslite 库路径
  - 详见 README.md

启动: python voice_bumi_client.py
"""

import asyncio
import json
import os
import sys
import logging
import signal
import threading
from pathlib import Path
from typing import Optional

import websockets
from websockets.asyncio.client import ClientConnection

# ── 日志 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bumi-client")

# ── 配置（环境变量作为默认值，命令行参数可覆盖）───────
SERVER_URL = os.environ.get("BUMI_SERVER_URL", "ws://192.168.2.168:8765")
ROBOT_ID = os.environ.get("BUMI_ROBOT_ID", "bumi-001")
DDS_XML_PATH = os.environ.get("BUMI_DDS_XML", str(Path(__file__).parent / "dds.xml"))


def parse_args():
    """解析命令行参数，优先级: 命令行 > 环境变量 > 默认值"""
    import argparse
    p = argparse.ArgumentParser(
        description="Bumi 机器人语音对话客户端 — DDS + WebSocket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n  python voice_bumi_client.py\n  python voice_bumi_client.py --server-url ws://localhost:8765 --robot-id bumi-002",
    )
    p.add_argument("--server-url", default=SERVER_URL,
                   help=f"WebSocket 服务端地址 (默认: %(default)s)")
    p.add_argument("--robot-id", default=ROBOT_ID,
                   help=f"机器人标识 (默认: %(default)s)")
    p.add_argument("--dds-xml", default=DDS_XML_PATH,
                   help=f"DDS 配置文件路径 (默认: %(default)s)")
    return p.parse_args()

# 项目根目录（pyVoiceCtrl.so 和 ddslite 所在）
PROJECT_ROOT = Path(__file__).parent.parent.parent  # bumiServerClient/client → bumiServerClient → noetix-voice-bumi
VOICE_BUMI_ROOT = PROJECT_ROOT / "noetix-voice-bumi"

# 添加 pyVoiceCtrl.so 所在目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent))

# 设置 DDS 库路径
dds_lib_path = VOICE_BUMI_ROOT / "ddslite" / "aarch64" / "lib"
if dds_lib_path.exists():
    ld_path = str(dds_lib_path) + ":" + os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = ld_path

# 设置 CycloneDDS 配置
os.environ["CYCLONEDDS_URI"] = f"file://{DDS_XML_PATH}"

# 重连配置
RECONNECT_DELAY_INITIAL = 1  # 初始重连延迟（秒）
RECONNECT_DELAY_MAX = 30     # 最大重连延迟
RECONNECT_DELAY_MULTIPLIER = 2


# ── 异步事件队列（C++ 线程 → asyncio 桥梁）────────────
class EventBridge:
    """将 pyVoiceCtrl 的回调（C++ 线程）桥接到 asyncio 事件循环"""

    def __init__(self):
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.queue: asyncio.Queue = asyncio.Queue()

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def push(self, event_type: str, data: str):
        """从任意线程安全推入事件"""
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (event_type, data))

    async def get(self):
        """异步获取事件"""
        return await self.queue.get()


# ── pyVoiceCtrl 封装 ──────────────────────────────────
class VoiceCtrl:
    """封装 pyVoiceCtrl.VoiceBumiCtrl，提供 DDS 通信能力"""

    def __init__(self):
        self._ctrl = None
        self._bridge: Optional[EventBridge] = None
        self._running = False

    @property
    def ctrl(self):
        return self._ctrl

    def _on_event(self, event_type: str, data: str):
        """C++ 回调入口（在 DDS 线程中调用）"""
        if self._bridge:
            self._bridge.push(event_type, data)

    def init(self, bridge: EventBridge) -> bool:
        """初始化 DDS 通信"""
        global pyVoiceCtrl
        try:
            import pyVoiceCtrl
        except ImportError as e:
            log.error(f"无法导入 pyVoiceCtrl: {e}")
            log.error("请确保 pyVoiceCtrl.so 在同目录，且 LD_LIBRARY_PATH 正确")
            return False

        self._bridge = bridge

        log.info(f"pyVoiceCtrl 版本: {pyVoiceCtrl.__version__}")
        log.info(f"构建信息: {pyVoiceCtrl.version_info}")

        self._ctrl = pyVoiceCtrl.VoiceBumiCtrl()

        # 关闭自动唤醒（由客户端显式控制）
        self._ctrl.set_auto_wakeup(False)

        # 初始化 DDS
        dds_path = DDS_XML_PATH if os.path.exists(DDS_XML_PATH) else ""
        log.info(f"DDS 配置: {dds_path or '(默认)'}")

        if not self._ctrl.init(dds_path, True):
            log.error("VoiceBumiCtrl 初始化失败")
            return False

        log.info("DDS 初始化成功")

        # 注册事件回调
        self._ctrl.set_event_callback(self._on_event)
        log.info("事件回调已注册")

        return True

    def load_configs(self):
        """加载默认配置到主控板"""
        log.info("加载默认配置...")
        self._ctrl.load_default_configs()

    def wakeup(self, need_audio: bool = True) -> bool:
        """唤醒机器人"""
        log.info("发送唤醒命令...")
        return self._ctrl.wakeup(need_audio)

    def sleep(self, need_audio: bool = True) -> bool:
        """休眠机器人"""
        log.info("发送休眠命令...")
        return self._ctrl.sleep(need_audio)

    def tts_text(self, text: str, interrupt: bool = True) -> bool:
        """让机器人说话"""
        mode = 1 if interrupt else 0
        return self._ctrl.tts_text(text, mode)

    def llm_text(self, text: str, interrupt: bool = True) -> bool:
        """发送文本到 LLM（备用，主控板内置 LLM）"""
        mode = 1 if interrupt else 0
        return self._ctrl.llm_text(text, mode)

    def volume_set(self, volume: int) -> bool:
        """设置音量 0-100"""
        return self._ctrl.volume_set(volume)

    def volume_get(self) -> bool:
        """查询当前音量"""
        return self._ctrl.volume_get()

    def audio_control(self, control_type: int) -> bool:
        """音频流控制"""
        return self._ctrl.audio_control(control_type)

    def is_running(self) -> bool:
        return self._ctrl.is_running() if self._ctrl else False

    def stop(self):
        """停止 DDS"""
        if self._ctrl:
            self._ctrl.stop()
        log.info("DDS 已停止")


# ── WebSocket 客户端 ───────────────────────────────────
class BumiClient:
    """Bumi 语音对话客户端主程序"""

    def __init__(self):
        self.ws: Optional[ClientConnection] = None
        self.voice: VoiceCtrl = VoiceCtrl()
        self.bridge: EventBridge = EventBridge()
        self._running = False
        self._awakened = False
        self._llm_busy = False  # LLM 正在处理中，防止并发
        self._pending_text = ""  # LLM 处理期间的积压文本

    async def run(self):
        """主入口：连接服务端 + 事件循环"""
        self._running = True

        # 初始化 DDS
        if not self.voice.init(self.bridge):
            log.error("DDS 初始化失败，退出")
            return

        # 设置事件循环引用
        self.bridge.set_loop(asyncio.get_event_loop())

        # 加载配置 + 唤醒
        self.voice.load_configs()
        await asyncio.sleep(2)  # 等待配置生效

        self.voice.volume_set(10)
        await asyncio.sleep(1)

        self._awakened = self.voice.wakeup()
        if not self._awakened:
            log.warning("唤醒可能失败，继续运行...")
        await asyncio.sleep(2)

        # 播放欢迎语
        welcome = "你好，我是小生来也！"
        log.info(f'TTS: "{welcome}"')
        self.voice.tts_text(welcome, interrupt=True)
        await asyncio.sleep(4)

        # WebSocket 连接循环
        reconnect_delay = RECONNECT_DELAY_INITIAL
        while self._running:
            try:
                await self._connect_and_process()
                reconnect_delay = RECONNECT_DELAY_INITIAL  # 正常断开，重置延迟
            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                log.warning(f"连接断开 [{type(e).__name__}]: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"连接异常 [{type(e).__name__}]: {e}")

            if not self._running:
                break

            log.info(f"{reconnect_delay}秒后重连...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * RECONNECT_DELAY_MULTIPLIER, RECONNECT_DELAY_MAX)

        log.info("客户端已停止")

    async def _connect_and_process(self):
        """建立 WebSocket 连接并处理双向消息"""
        log.info(f"连接到服务端: {SERVER_URL}")
        async with websockets.connect(
            SERVER_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self.ws = ws
            log.info(f"已连接到 {SERVER_URL}")

            # 发送握手
            await ws.send(json.dumps({"type": "hello", "robot_id": ROBOT_ID}, ensure_ascii=False))

            # 并行处理：接收服务端消息 + DDS 事件
            await asyncio.gather(
                self._recv_ws(ws),
                self._process_dds_events(ws),
            )

    async def _recv_ws(self, ws: ClientConnection):
        """接收服务端消息并执行"""
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(f"无效 JSON: {raw[:100]}")
                continue

            msg_type = msg.get("type", "")

            if msg_type == "ready":
                log.info(f"服务端就绪: robot_id={msg.get('robot_id', '?')}")

            elif msg_type == "tts":
                text = msg.get("text", "")
                interrupt = msg.get("interrupt", False)
                if text:
                    log.info(f"TTS: {text[:80]}...")
                    self.voice.tts_text(text, interrupt=interrupt)
                self._llm_busy = False
                # 处理 LLM 期间的积压文本
                if self._pending_text:
                    pending = self._pending_text
                    self._pending_text = ""
                    log.info(f"处理积压文本: {pending[:50]}")
                    asyncio.create_task(self._send_asr(ws, pending))

            elif msg_type == "llm_stream":
                # 流式 token (可用于实时字幕显示)
                token = msg.get("token", "")
                if token:
                    pass  # 可选：打印到控制台

            elif msg_type == "llm_done":
                log.debug("LLM 回复完成")

            elif msg_type == "command":
                cmd = msg.get("command", "")
                params = msg.get("params", {})
                await self._execute_command(cmd, params)

            elif msg_type == "error":
                log.error(f"服务端错误: {msg.get('message', '')}")
                self._llm_busy = False

            elif msg_type == "interrupt_ack":
                log.info("打断已确认")
                self._llm_busy = False

            elif msg_type == "pong":
                pass

            else:
                log.debug(f"未知消息类型: {msg_type}")

    async def _process_dds_events(self, ws: ClientConnection):
        """处理 DDS 事件（C++ 回调 → asyncio 队列）"""
        while self._running and self.voice.is_running():
            try:
                event_type, data = await asyncio.wait_for(self.bridge.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                if event_type == "SUBTITLE":
                    await self._handle_subtitle(ws, data)

                elif event_type == "AWAKEN":
                    self._awakened = True
                    log.info(f"语音唤醒: {data}")

                elif event_type == "CONVERSATION_STATUS":
                    await self._handle_conversation_status(ws, data)

                elif event_type in ("COMMAND", "INFO"):
                    log.debug(f"[{event_type}] {data}")

                elif event_type == "ERROR":
                    log.error(f"DDS 错误: {data}")

                else:
                    log.debug(f"[{event_type}] {data[:100]}")

            except Exception as e:
                log.error(f"处理 DDS 事件异常 [{type(e).__name__}]: {e}")

    async def _handle_subtitle(self, ws: ClientConnection, data: str):
        """处理 ASR 字幕，转发给服务端"""
        try:
            result = json.loads(data)
            items = result.get("data", [])
            if items:
                text = items[0].get("text", "")
                definite = items[0].get("definite", False)

                if definite:
                    # 最终识别结果 → 发送给 LLM 服务端
                    log.info(f"ASR 最终: {text}")
                    await self._send_asr(ws, text)
                else:
                    # 中间结果（仅日志）
                    log.info(f"ASR 中间: {text}")
        except (json.JSONDecodeError, IndexError):
            pass

    async def _send_asr(self, ws: ClientConnection, text: str):
        """发送 ASR 结果到服务端"""
        if self._llm_busy:
            # LLM 正在处理，积压此文本，等处理完再发
            self._pending_text = text
            log.info(f"LLM 忙碌，积压文本: {text[:50]}")
            return

        self._llm_busy = True
        try:
            await ws.send(json.dumps({
                "type": "asr_result",
                "text": text,
                "definite": True,
            }, ensure_ascii=False))
        except Exception as e:
            log.error(f"发送 ASR 失败: {e}")
            self._llm_busy = False

    async def _handle_conversation_status(self, ws: ClientConnection, data: str):
        """处理对话状态变化"""
        try:
            status = json.loads(data)
            stage = status.get("Stage", {})
            code = stage.get("Code", 0)
            desc = stage.get("Description", "")
            log.info(f"对话状态: {desc} (code={code})")
        except json.JSONDecodeError:
            pass

    async def _execute_command(self, cmd: str, params: dict):
        """执行服务端下发的机器人控制指令"""
        if cmd == "sleep":
            self._awakened = False
            self.voice.sleep()
            await asyncio.sleep(1)

        elif cmd == "volume_up":
            # 每次上调 10
            self.voice.volume_set(min(100, 80))  # 简化：当前音量未知，固定设 80
            log.info("音量已调大")

        elif cmd == "volume_down":
            self.voice.volume_set(30)
            log.info("音量已调小")

        elif cmd == "volume_to":
            vol = params.get("volume", 50)
            vol = max(0, min(100, vol))
            self.voice.volume_set(vol)
            log.info(f"音量已设置为 {vol}")

        elif cmd == "reset":
            self.voice.sleep()
            await asyncio.sleep(2)
            self.voice.wakeup()
            self._awakened = True

        else:
            log.warning(f"未知指令: {cmd}")

    async def shutdown(self):
        """优雅关闭"""
        log.info("正在关闭...")
        self._running = False

        # 休眠机器人
        if self._awakened:
            self.voice.sleep()
            await asyncio.sleep(1)

        self.voice.stop()

        if self.ws:
            await self.ws.close()
            self.ws = None


# ── 入口 ───────────────────────────────────────────────
async def main():
    client = BumiClient()

    # 注册信号处理
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(client.shutdown())
        )

    try:
        await client.run()
    except KeyboardInterrupt:
        pass
    finally:
        await client.shutdown()

    log.info("再见！")


# ── 入口 ───────────────────────────────────────────────
if __name__ == "__main__":
    args = parse_args()
    # 命令行参数覆盖全局配置
    SERVER_URL = args.server_url
    ROBOT_ID = args.robot_id
    DDS_XML_PATH = args.dds_xml

    asyncio.run(main())
