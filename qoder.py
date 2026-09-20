"""Qoder Flash 最小客户端：读本机凭据，只跑 Qwen3.8-Flash。

协议是按实测/逆向得来的，不是官方文档：

  凭据  `~/.qoder/.auth/machine_id`（明文）与 `~/.qoder/.auth/user`
        = base64(AES-CBC/PKCS7)，密钥和 IV 都是 machine_id 的前 16 字节。
        这两个文件由 Qoder CLI 执行 `qodercli login` 后写下；本程序**只读不写**。

  请求  POST api3.qoder.sh/.../agent_chat_generation（老版 SSE 端点）
        - 鉴权：`authorization: Bearer COSY.<payload>.<md5>`，外加一组 cosy-* 头
        - 包体：JSON 序列化后还要过一遍 Qoder 自定义 base64（自有字母表 + 三段重排）
        - 模型：放在请求头 `X-Model-Key` 里，Qwen3.8-Flash = `qfmodel`
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

AUTH_DIR = Path.home() / ".qoder" / ".auth"

CHAT_URL = (
    "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation"
    "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
)

MODEL_KEY = "qfmodel"  # Qwen3.8-Flash 在 Qoder 侧的 key（抓 CLI 请求头 X-Model-Key 所得）
MODEL_NAME = "qwen3.8-flash"
MODEL_DISPLAY = "Qwen3.8-Flash"

COSY_VERSION = "1.1.58"
COSY_SECRET = "d2FyLCB3YXIgbmV2ZXIgY2hhbmdlcw=="  # base64("war, war never changes")

# 老版协议用它加密每次请求的临时密钥（cosy-key 头）
SERVER_PUBKEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""

# Qoder 自定义 base64：标准字母表映射到自有字母表，并把字符串切成三段重排
CUSTOM_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
STANDARD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
CUSTOM_PAD = "$"
_TO_CUSTOM = str.maketrans(STANDARD_ALPHABET + "=", CUSTOM_ALPHABET + CUSTOM_PAD)


class QoderError(RuntimeError):
    """请求 Qoder 失败（网络/鉴权/服务端错误）。"""


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    uid: str
    name: str
    user_type: str
    machine_id: str
    token: str  # security_oauth_token，dt- 前缀
    refresh_token: str  # drt- 前缀
    expires_at: int  # unix 秒，0 表示未知


def load_credentials(auth_dir: Path | None = None) -> Credentials:
    """解密本机 Qoder CLI 写下的登录凭据。"""
    auth_dir = auth_dir or AUTH_DIR
    id_path = auth_dir / "id"
    if not id_path.exists():
        id_path = auth_dir / "machine_id"
    user_path = auth_dir / "user"
    if not id_path.exists() or not user_path.exists():
        raise QoderError(
            f"没找到 Qoder 凭据：{auth_dir} 下需要 machine_id（或 id）和 user 两个文件。"
            "先在本机执行一次 `qodercli login`。"
        )

    machine_id = id_path.read_text(encoding="utf-8").strip()
    blob = base64.b64decode(user_path.read_text(encoding="utf-8").strip())
    key = machine_id[:16].encode("ascii")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    padded = decryptor.update(blob) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    data = json.loads((unpadder.update(padded) + unpadder.finalize()).decode("utf-8"))

    return Credentials(
        uid=str(data.get("uid") or ""),
        name=str(data.get("name") or ""),
        user_type=str(data.get("user_type") or "personal_standard"),
        machine_id=machine_id,
        token=str(data.get("security_oauth_token") or data.get("securityOauthToken") or ""),
        refresh_token=str(data.get("refresh_token") or ""),
        expires_at=int(data.get("expire_time") or 0),
    )


# ---------------------------------------------------------------------------
# 编码 / 签名
# ---------------------------------------------------------------------------


def encode_body(plaintext: bytes) -> str:
    """Qoder 自定义 base64：标准 base64 → 三段重排 → 换字母表。"""
    standard = base64.b64encode(plaintext).decode("ascii")
    split = len(standard) // 3
    return (standard[-split:] + standard[split:-split] + standard[:split]).translate(_TO_CUSTOM)


def _rsa_encrypt(plain: bytes) -> bytes:
    return serialization.load_pem_public_key(SERVER_PUBKEY_PEM).encrypt(plain, asym_padding.PKCS1v15())


def _aes_cbc_encrypt(plain: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    data = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _new_machine() -> tuple[str, str]:
    """每次请求随机的机器指纹（服务端不校验其来源）。"""
    machine_token = base64.urlsafe_b64encode(uuid.uuid4().hex.encode()).decode().rstrip("=")[:50]
    return machine_token, uuid.uuid4().hex[:18]


def build_headers(cred: Credentials, body: str) -> dict[str, str]:
    """构造老版协议的 COSY 鉴权头；签名覆盖 body 与路径。"""
    temp_key = uuid.uuid4().hex[:16].encode("ascii")
    cosy_key = base64.b64encode(_rsa_encrypt(temp_key)).decode()

    identity = {
        "name": cred.name,
        "aid": cred.uid,
        "uid": cred.uid,
        "yx_uid": "",
        "organization_id": "",
        "organization_name": "",
        "user_type": cred.user_type,
        "security_oauth_token": cred.token,
        "refresh_token": cred.refresh_token,
    }
    info = base64.b64encode(
        _aes_cbc_encrypt(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode(), temp_key)
    ).decode()
    outer = {"cosyVersion": COSY_VERSION, "ideVersion": "", "info": info, "requestId": str(uuid.uuid4()), "version": "v1"}
    payload_b64 = base64.b64encode(
        json.dumps(dict(sorted(outer.items())), separators=(",", ":")).encode()
    ).decode()

    date = str(int(time.time()))
    path = urlparse(CHAT_URL).path
    path_sig = path[len("/algo"):] if path.startswith("/algo") else path
    signature = hashlib.md5(f"{payload_b64}\n{cosy_key}\n{date}\n{body}\n{path_sig}".encode()).hexdigest()

    machine_token, machine_type = _new_machine()
    return {
        "authorization": f"Bearer COSY.{payload_b64}.{signature}",
        "content-type": "application/json",
        "accept": "text/event-stream",
        "accept-encoding": "identity",
        "cache-control": "no-cache",
        "cosy-data-policy": "AGREE",
        "cosy-date": date,
        "cosy-key": cosy_key,
        "cosy-user": cred.uid,
        "cosy-clienttype": "5",
        "cosy-clientip": "169.254.198.161",
        "cosy-version": COSY_VERSION,
        "cosy-machineid": cred.machine_id,
        "cosy-machinetoken": machine_token,
        "cosy-machinetype": machine_type,
        "login-version": "v2",
        "x-model-key": MODEL_KEY,
        "x-model-source": "system",
        "user-agent": "Go-http-client/2.0",
    }


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


def _response_meta() -> dict[str, Any]:
    return {
        "id": "",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "completion_tokens_details": {"reasoning_tokens": 0},
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


def _text_of(content: Any) -> str:
    """把 OpenAI 的 content（字符串或分段数组）拍平成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return "" if content is None else str(content)


def _convert_message(message: dict[str, Any]) -> dict[str, Any] | None:
    role = str(message.get("role") or "user")
    text = _text_of(message.get("content"))

    if role == "tool":  # 工具结果在无 tools 时当作上下文文本回灌
        label = "Tool result"
        if message.get("name"):
            label += f" ({message['name']})"
        text = f"{label}:\n{text}" if text.strip() else label
        role = "user"

    if role == "user":
        if not text.strip():
            return None
        return {
            "role": "user",
            "content": "",
            "contents": [{"type": "text", "text": text}],
            "response_meta": _response_meta(),
            "reasoning_content_signature": "",
        }
    if role == "assistant" and message.get("tool_calls"):
        text = f"{text}\n\nTool calls:\n{json.dumps(message['tool_calls'], ensure_ascii=False)}".strip()
    if not text.strip():
        return None
    return {
        "role": role,
        "content": text,
        "response_meta": _response_meta(),
        "reasoning_content_signature": "",
    }


def build_body(
    cred: Credentials,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    reasoning_effort: str = "none",
) -> dict[str, Any]:
    """拼出老版端点的请求体（字段名照服务端要求，不能随意增删）。"""
    converted = [m for m in (_convert_message(m) for m in messages) if m]
    prompt = ""
    for message in reversed(converted):
        if message["role"] == "user":
            prompt = message.get("content") or (message.get("contents") or [{}])[0].get("text", "")
            break

    request_id = str(uuid.uuid4())
    return {
        "request_id": request_id,
        "request_set_id": str(uuid.uuid4()),
        "chat_record_id": request_id,
        "session_id": str(uuid.uuid4()),
        "stream": True,
        "chat_task": "FREE_INPUT",
        "chat_context": {
            "chatPrompt": "",
            "extra": {
                "context": [],
                "modelConfig": {"is_reasoning": False, "key": MODEL_KEY},
                "originalContent": {"type": "text", "text": prompt},
            },
            "features": [],
            "imageUrls": None,
            "text": {"type": "text", "text": prompt},
        },
        "image_urls": None,
        "is_reply": True,
        "is_retry": False,
        "code_language": "",
        "source": 1,
        "version": "3",
        "chat_prompt": "",
        # reasoning_effort 必须显式给值：不给的话服务端默认长思考，
        # 一句「说三个字」也要 30 秒以上；给 none 后实测 0.8 秒。
        "parameters": {"max_tokens": 32768, "reasoning_effort": reasoning_effort},
        "aliyun_user_type": cred.user_type,
        "session_type": "qodercli",
        "agent_id": "agent_common",
        "task_id": "common",
        "model_config": {
            "key": MODEL_KEY,
            "display_name": MODEL_DISPLAY,
            "model": "",
            "format": "openai",
            "is_vl": False,
            "is_reasoning": reasoning_effort != "none",
            "api_key": "",
            "url": "",
            "source": "system",
            "max_input_tokens": 180000,
        },
        "messages": converted,
        "tools": tools or [],
        "business": {
            "product": "cli",
            "version": COSY_VERSION,
            "type": "agent",
            "id": str(uuid.uuid4()),
            "name": prompt[:30],
            "begin_at": int(time.time() * 1000),
            "stage": "start",
        },
    }


# ---------------------------------------------------------------------------
# 流式调用
# ---------------------------------------------------------------------------


def _parse_chunk(raw: str) -> list[dict[str, Any]]:
    """解析一行 SSE data：老版会把 OpenAI chunk 塞在 {"body": "<json>"} 里。"""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    if payload.get("stackTrace"):  # 服务端异常，直接抛出便于排查
        raise QoderError(f"Qoder 服务端异常：{json.dumps(payload, ensure_ascii=False)[:300]}")
    inner = payload.get("body")
    if isinstance(inner, str):
        try:
            payload = json.loads(inner)
        except json.JSONDecodeError:
            return []

    out: list[dict[str, Any]] = []
    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        content = delta.get("content")
        reasoning = delta.get("reasoning_content")
        if content or reasoning or delta.get("role") or delta.get("tool_calls") or choice.get("finish_reason"):
            out.append(
                {
                    "role": delta.get("role") or "",
                    "content": content or "",
                    "reasoning": reasoning or "",
                    "tool_calls": delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else None,
                    "finish_reason": choice.get("finish_reason"),
                }
            )
    return out


async def stream_chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    cred: Credentials | None = None,
    timeout: float = 300.0,
    reasoning_effort: str = "none",
) -> AsyncIterator[dict[str, Any]]:
    """向 Qoder 发起一次流式对话，逐块吐出 delta。"""
    cred = cred or load_credentials()
    body = build_body(cred, messages, tools, reasoning_effort)
    encoded = encode_body(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
    headers = build_headers(cred, encoded)

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15)) as client:
        async with client.stream("POST", CHAT_URL, content=encoded.encode(), headers=headers) as response:
            if response.status_code != 200:
                detail = (await response.aread()).decode(errors="replace")
                if response.status_code in (401, 403):
                    raise QoderError(f"凭据被拒（HTTP {response.status_code}）：{detail}。凭据可能已过期，重新 `qodercli login` 一次。")
                raise QoderError(f"HTTP {response.status_code} {detail}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                for delta in _parse_chunk(raw):
                    yield delta


def merge_tool_calls(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把工具调用碎片拼回完整调用。

    老版协议会把一次调用拆成好几片：id/函数名在第一片，arguments 被切成几段跟在后面
    （且每片的 index 都是 0，所以不能按 index 合并，只能按到达顺序拼）。
    """
    merged: list[dict[str, Any]] = []
    for fragment in fragments:
        function = fragment.get("function") or {}
        name = str(function.get("name") or "")
        arguments = str(function.get("arguments") or "")
        starts_new = bool(fragment.get("id")) and (not merged or name)
        if not merged or starts_new:
            merged.append(
                {
                    "id": str(fragment.get("id") or ""),
                    "type": str(fragment.get("type") or "function"),
                    "function": {"name": name, "arguments": arguments},
                }
            )
            continue
        if name and not merged[-1]["function"]["name"]:
            merged[-1]["function"]["name"] = name
        merged[-1]["function"]["arguments"] += arguments
    return merged


async def complete_chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    cred: Credentials | None = None,
    reasoning_effort: str = "none",
) -> dict[str, Any]:
    """非流式：把流式结果拼成一条完整回复。"""
    parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    async for delta in stream_chat(messages, tools, cred, reasoning_effort=reasoning_effort):
        if delta["content"]:
            parts.append(delta["content"])
        if delta["reasoning"]:
            reasoning_parts.append(delta["reasoning"])
        if delta["tool_calls"]:
            tool_calls.extend(delta["tool_calls"])
    message: dict[str, Any] = {"role": "assistant", "content": "".join(parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = merge_tool_calls(tool_calls)
    return message


def credential_summary(cred: Credentials) -> dict[str, Any]:
    """给 /health 用的凭据概览（不含任何密钥）。"""
    expired = bool(cred.expires_at) and cred.expires_at < time.time()
    return {
        "uid": cred.uid,
        "name": cred.name,
        "user_type": cred.user_type,
        "token_expired": expired,
        "expires_at": cred.expires_at or None,
    }
