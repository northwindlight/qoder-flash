# Qoder Flash Gateway

把本机的 Qoder 登录态包成一个 OpenAI 兼容接口，固定只跑 **Qwen3.8-Flash**。
Windows / Linux 通用（纯 Python，无平台相关代码）。

## 它是怎么工作的

1. 读 `qodercli login` 在 `~/.qoder/.auth/` 下写好的两个文件：`machine_id`（明文）和 `user`
   （base64 的 AES-CBC 密文，密钥与 IV 都取 `machine_id` 前 16 字节）。**只读，不写、不改**，
   所以不会动到 CLI 自己的登录态。
2. 用这套凭据向 Qoder 的老版 SSE 端点发起请求：
   `api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation`
   —— 鉴权是 COSY 签名头，包体要过一遍 Qoder 自定义 base64，模型名放在 `X-Model-Key: qfmodel`。
3. 把回来的流翻译成 OpenAI 的 `chat.completion.chunk`。

为什么走老版端点：免费账号的 **Qwen3.8-Flash 是无限量的**，而这条路是唯一能用到它的
（新版 OpenAI 兼容端点对这类账号只放行 `lite`，其余模型一律 `402 quota exceeded`）。

> 上面这套协议是怎么逆出来的、走过哪些死路、版本更新后怎么重来一遍 —— 见
> [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md)。

## 安装与运行

```bash
python -m venv .venv
# Linux/macOS
.venv/bin/pip install -r requirements.txt
# Windows
.venv\Scripts\pip install -r requirements.txt

# Linux/macOS
.venv/bin/python main.py
# Windows
.venv\Scripts\python main.py
```

默认监听 `127.0.0.1:5050`。前置条件是本机已经登录过 Qoder CLI：

```bash
npm install -g @qoder-ai/qodercli
qodercli login
```

## 接口

请求/响应的**形状**按 DeepSeek 那套来（DeepSeek 本身是 OpenAI 兼容，所以 OpenAI 客户端也能直接用），
模型名固定是 **`qwen3.8-flash`**：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat/completions` | DeepSeek 官方 base_url 形式 |
| POST | `/v1/chat/completions` | OpenAI SDK 形式 |
| GET | `/models`、`/v1/models` | `qwen3.8-flash` |
| GET | `/health` | 是否就绪 + 凭据概览（不含密钥） |

```bash
curl http://127.0.0.1:5050/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
        "model": "qwen3.8-flash",
        "messages": [{"role": "user", "content": "你好"}],
        "stream": false
      }'
```

当 OpenAI / DeepSeek 客户端接：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:5050", api_key="unused")  # 没设 QODER_API_KEY 时 key 随便填
client.chat.completions.create(model="qwen3.8-flash", messages=[{"role": "user", "content": "你好"}])
```

`model` 字段只用于回显（填别的名字也能跑，只是不推荐）。

## 思考强度

模型本身支持思考，档位用 DeepSeek 那套参数控制：

| 参数 | 作用 |
|---|---|
| `reasoning_effort` | `none` / `low` / `medium` / `high` / `xhigh` / `max`，直接定档 |
| `thinking: {"type": "enabled"}` | 只开不定档，等于 `medium`；`disabled` 等于 `none` |
| 都不给 | 默认 `none`（关思考最快） |

开了思考时，思维链走 DeepSeek 的 `reasoning_content` 字段回来（流式里是逐块的 delta）。

同一道题（9.11 vs 9.9）实测：

| effort | 耗时 | 思考字数 | 结果 |
|---|---|---|---|
| `none` | 1.2s | 0 | ❌ 答成 9.11 大 |
| `low` | 5.5s | 1235 | ✅ |
| `medium` | 6.4s | 1421 | ✅ |
| `high` | 4.1s | 768 | ✅ |

越往上思考越长（`max` 实测 3293 字思考、15s），按需要选。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `QODER_HOST` | `127.0.0.1` | 监听地址（给局域网用就设 `0.0.0.0`） |
| `QODER_PORT` | `5050` | 端口（也可用 `--port`） |
| `QODER_API_KEY` | 空 | 设了之后调用必须带 `Authorization: Bearer <key>`；对局域网暴露时建议设 |

## 开机自启

Linux（systemd）：

```ini
# /etc/systemd/system/qoder-flash.service
[Unit]
Description=Qoder Flash Gateway
After=network-online.target

[Service]
User=<你的用户名>
WorkingDirectory=<项目绝对路径>
ExecStart=<项目绝对路径>/.venv/bin/python main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Windows：`python main.py` 直接用，或用「任务计划程序」加一个「登录时/开机时」触发的任务。

## 已知限制

- **不做 token 自动刷新**：`dt-` 过期后（`/health` 会显示 `token_expired: true`）重新
  `qodercli login` 一次即可。刻意不做刷新是因为刷新可能轮换掉 CLI 自己那份凭据，
  把 CLI 弄掉线——只读更安全。
- 只跑 Flash，不支持切模型（免费账号上别的模型会 402）。
- `usage` 里的 token 统计恒为 0：老版响应里没有可直接用的计数。
- 协议是逆向来的，Qoder 改服务端就可能失效；失效时先看 `/health` 和返回的错误详情。
