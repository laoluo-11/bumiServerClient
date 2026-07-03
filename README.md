# Bumi Server-Client — 绕过豆包大模型的语音对话方案

## 架构

```
┌─ Jetson 算力板 (192.168.55.101) ──────────┐
│  voice_bumi_client.py                      │
│  ├─ pyVoiceCtrl.so → DDS ↔ 主控板          │
│  └─ WebSocket ─────────────┐               │
└────────────────────────────┼───────────────┘
                             │
                             ▼
┌─ 远程服务器 (192.168.2.168) ───────────────┐
│  voice_bumi_server.py                      │
│  └─ OpenAI 兼容 API → DeepSeek / 任意模型   │
└────────────────────────────────────────────┘
```

## 依赖关系

客户端依赖 `noetix-voice-bumi` 项目提供的 DDS 运行时库，两个项目需在同一父目录下：

```
work_bumi/
├── bumiServerClient/          # 本项目
│   ├── server/
│   └── client/
└── noetix-voice-bumi/         # DDS 库 + pyVoiceCtrl 编译依赖
    └── ddslite/aarch64/lib/   # libddsc.so 等
```

## 目录结构

```
bumiServerClient/
├── server/
│   ├── voice_bumi_server.py   # 服务端：WebSocket + LLM
│   ├── requirements.txt
│   └── .env.example
├── client/
│   ├── voice_bumi_client.py   # 客户端：DDS + WebSocket
│   ├── requirements.txt
│   ├── pyVoiceCtrl.cpython-312-aarch64-linux-gnu.so
│   ├── pyVoiceCtrl.cpython-310-aarch64-linux-gnu.so
│   └── dds.xml                # DDS 配置
└── README.md
```

## 快速开始

### 1. 服务端（远程服务器上运行）

```bash
cd bumiServerClient/server
pip install -r requirements.txt

# 设置 API Key（二选一）
export DEEPSEEK_API_KEY=***          # DeepSeek
export BUMI_LLM_API_KEY=***          # 通用 OpenAI 兼容

# 可选：切换模型
export BUMI_LLM_MODEL="deepseek-chat"     # 默认
export BUMI_LLM_BASE_URL="https://api.deepseek.com"

# 启动服务端
python voice_bumi_server.py
```

### 2. 客户端（Jetson 算力板上运行）

```bash
# SSH 到 Jetson
ssh noetix@192.168.55.101

# 将整个 bumiServerClient 目录拷贝到 Jetson
# （或至少拷贝 client/ 目录）

cd bumiServerClient/client
pip install -r requirements.txt

# 设置 DDS 库路径
export LD_LIBRARY_PATH=/home/noetix/work_bumi/noetix-voice-bumi/ddslite/aarch64/lib:$LD_LIBRARY_PATH

# 设置服务端地址
export BUMI_SERVER_URL="ws://192.168.2.168:8765"

# 启动客户端
python voice_bumi_client.py
```

## WebSocket 消息协议

### 客户端 → 服务端

| type | 说明 | 参数 |
|------|------|------|
| `hello` | 握手 | `robot_id` |
| `asr_result` | ASR 最终识别文本 | `text`, `definite: true` |
| `interrupt` | 打断当前 LLM 输出 | — |
| `ping` | 心跳 | — |

### 服务端 → 客户端

| type | 说明 | 参数 |
|------|------|------|
| `ready` | 握手确认 | `robot_id` |
| `tts` | TTS 播报文本 | `text`, `interrupt` |
| `llm_stream` | 流式 token | `token` |
| `llm_done` | LLM 回复完成 | — |
| `command` | 机器人控制指令 | `command` (sleep/volume_up/...), `params` |
| `error` | 错误信息 | `message` |
| `interrupt_ack` | 打断确认 | — |

## 配置项

所有配置均通过环境变量设置：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BUMI_SERVER_HOST` | `0.0.0.0` | 服务端监听地址 |
| `BUMI_SERVER_PORT` | `8765` | 服务端端口 |
| `BUMI_SERVER_URL` | `ws://192.168.2.168:8765` | 客户端连接地址 |
| `BUMI_LLM_BASE_URL` | `https://api.deepseek.com` | LLM API 地址 |
| `BUMI_LLM_API_KEY` | `$DEEPSEEK_API_KEY` | LLM API Key |
| `BUMI_LLM_MODEL` | `deepseek-chat` | 模型名称 |
| `BUMI_STREAMING` | `true` | 是否流式输出 |
| `BUMI_MAX_HISTORY` | `20` | 最大对话轮数 |
| `BUMI_ROBOT_ID` | `bumi-001` | 机器人标识 |
| `BUMI_DDS_XML` | `./dds.xml` | DDS 配置路径 |

## 命令行参数

所有配置项都可通过命令行参数指定，优先级: 命令行 > 环境变量 > 默认值。

### 服务端 (voice_bumi_server.py)

```
python voice_bumi_server.py --help
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--host` | str | `0.0.0.0` | 监听地址 |
| `--port` | int | `8765` | 监听端口 |
| `--model` | str | `deepseek-chat` | LLM 模型名称 |
| `--base-url` | str | `https://api.deepseek.com` | LLM API 地址 |
| `--api-key` | str | 环境变量 | LLM API Key |
| `--max-history` | int | `20` | 最大对话轮数 |
| `--no-stream` | flag | — | 禁用流式输出 |

用法示例：

```bash
# 使用默认配置启动
python voice_bumi_server.py

# 指定端口和模型
python voice_bumi_server.py --port 9000 --model gpt-4o

# 使用 OpenAI API（同时指定 key 和地址）
python voice_bumi_server.py --api-key sk-xxx --base-url https://api.openai.com/v1

# 禁用流式输出
python voice_bumi_server.py --no-stream
```

### 客户端 (voice_bumi_client.py)

```
python voice_bumi_client.py --help
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--server-url` | str | `ws://192.168.2.168:8765` | WebSocket 服务端地址 |
| `--robot-id` | str | `bumi-001` | 机器人标识 |
| `--dds-xml` | str | `./dds.xml` | DDS 配置文件路径 |

用法示例：

```bash
# 使用默认配置启动
python voice_bumi_client.py

# 连接本地服务端
python voice_bumi_client.py --server-url ws://localhost:8765

# 指定机器人 ID
python voice_bumi_client.py --robot-id bumi-002

# 指定 DDS 配置文件
python voice_bumi_client.py --dds-xml /path/to/custom_dds.xml
```

## 切换 LLM 模型

兼容 OpenAI API 格式的都可以，只需改环境变量：

```bash
# DeepSeek（默认）
export BUMI_LLM_BASE_URL="https://api.deepseek.com"
export BUMI_LLM_MODEL="deepseek-chat"

# 通义千问
export BUMI_LLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export BUMI_LLM_API_KEY=***
export BUMI_LLM_MODEL="qwen-plus"

# OpenAI
export BUMI_LLM_BASE_URL="https://api.openai.com/v1"
export BUMI_LLM_API_KEY=***
export BUMI_LLM_MODEL="gpt-4o"

# 任何兼容接口
export BUMI_LLM_BASE_URL="http://your-server:8000/v1"
export BUMI_LLM_MODEL="your-model"
```

## 数据流

```
用户说话 → 主控板 ASR → DDS subtitle → pyVoiceCtrl 回调
    → EventBridge → asyncio Queue → WebSocket → 远程服务端
    → DeepSeek LLM → 流式回复 → WebSocket
    → asyncio → DDS tts_text → 主控板 TTS → 机器人说话
```

## DDS Topic 通信

客户端通过 pyVoiceCtrl.so（CycloneDDS）与主控板（192.168.55.102）通信，所有 Topic 命名空间为 `/noetix/bumi/agent/`。

### 发布 Topic（客户端 → 主控板）

| Topic | 用途 | 对应代码方法 |
|-------|------|-------------|
| `system/control` | 唤醒/休眠/复位 (Wake=0, Sleep=1, Reset=2) | `wakeup()`, `sleep()` |
| `tts/text_input` | 发送文本给主控板 TTS 播报 | `tts_text(text)` |
| `llm/text_input` | 发送文本给主控板内置 LLM | `llm_text(text)` |
| `audio/volume/set` | 设置音量 (0-100) | `volume_set(vol)` |
| `audio/volume/get` | 查询音量 | `volume_get()` |
| `conversation/interrupt` | 打断当前播报 (barge-in) | `tts_text(..., interrupt=True)` |

> 初始化时还会通过 `load_default_configs()` 写入 LLM/ASR/TTS 默认配置（`llm/config/set`, `asr/config/set`, `tts/config/set`）。

### 订阅 Topic（主控板 → 客户端）

| Topic | 用途 | 回调事件类型 |
|-------|------|-------------|
| `conversation/subtitle` | ASR 识别文本（用户说的话） | `SUBTITLE` |
| `system/status` | 系统状态变化（唤醒/休眠等） | `AWAKEN` |
| `functioncall/command` | 主控板 LLM function call 指令 | (未在客户端处理, 服务端发出 `command` 消息替代) |
| `functioncall/info` | function call 附带数据 | — |

### 通信模式

**请求-响应模式**（config/set, config/get）：主控板 C++ SDK 内部通过 message_id 匹配 + condition_variable 超时机制实现同步等待，pyVoiceCtrl 封装了此逻辑。

**事件驱动模式**（subtitle, status）：C++ DDS reader 线程收到消息后触发回调，客户端通过 EventBridge（`call_soon_threadsafe` + `asyncio.Queue`）将事件安全转入 asyncio 事件循环处理。

### DDS 配置 (dds.xml)

```xml
<CycloneDDS>
    <Domain id="any">
        <General>
            <Transport>udp</Transport>
            <AllowMulticast>false</AllowMulticast>
        </General>
        <Discovery>
            <Peers>
                <Peer address="192.168.55.102"/>  <!-- 主控板 -->
                <Peer address="127.0.0.1"/>        <!-- 本机 -->
            </Peers>
        </Discovery>
    </Domain>
</CycloneDDS>
```

配置文件位于 `client/dds.xml`，客户端启动时通过 `CYCLONEDDS_URI` 环境变量或 `--dds-xml` 参数指定。

## 注意事项

1. **pyVoiceCtrl.so 是 aarch64 二进制**，只能在 Jetson 上加载，不能在 x86 服务器上运行客户端。
2. 服务端可以跑在任何能访问 LLM API 的机器上，不需要 pyVoiceCtrl。
3. 客户端运行时需要 LD_LIBRARY_PATH 包含 CycloneDDS 的 .so 库（来自 noetix-voice-bumi 项目）。代码会自动检测 `../noetix-voice-bumi/ddslite/aarch64/lib` 路径，也可手动设置。
4. dds.xml 中需要配置主控板的 IP（默认 192.168.55.102）。
5. 客户端和 noetix-voice-bumi 项目需在同一父目录下，详见「依赖关系」一节。
6. pyVoiceCtrl.cpython-3xx-aarch64-linux-gnu.so 放在 `client/` 目录下，与 `voice_bumi_client.py` 同级。Python 会根据解释器版本自动选择 `.cpython-310`（Python 3.10）或 `.cpython-312`（Python 3.12）。
