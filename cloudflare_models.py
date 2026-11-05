"""Cloudflare Workers AI clients shared by the app and notebooks."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TEXT_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"


def _configuration() -> tuple[str, str]:
    api_key = os.getenv("CLOUDFLARE_API_KEY", "").strip()
    base_url = os.getenv("CLOUDFLARE_BASEURL", "").strip().rstrip("/")
    if not api_key or not base_url:
        raise ValueError(
            "请在 AgentWriter/.env 中配置 CLOUDFLARE_API_KEY 和 CLOUDFLARE_BASEURL。"
        )
    if not base_url.endswith("/ai/v1"):
        raise ValueError("CLOUDFLARE_BASEURL 必须以 /ai/v1 结尾。")
    return api_key, base_url


def create_chat_model() -> ChatOpenAI:
    api_key, base_url = _configuration()
    return ChatOpenAI(
        model=TEXT_MODEL,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
        max_tokens=8192,
        timeout=180,
        max_retries=2,
        use_responses_api=False,
    )


def structured_output(llm: ChatOpenAI, schema: type[BaseModel]):
    # Workers AI expects the JSON Schema directly, without OpenAI's
    # {name, strict, schema} wrapper used by with_structured_output().
    parser = PydanticOutputParser(pydantic_object=schema)
    # Include field descriptions in the prompt as well as constraining the
    # response: the model needs the nested fields to plan useful content.
    prompt = RunnableLambda(lambda messages: [
        SystemMessage(content=parser.get_format_instructions()), *messages,
    ])
    return prompt | llm.bind(
        response_format={
            "type": "json_schema",
            "json_schema": schema.model_json_schema(),
        }
    ) | parser


def generate_image_bytes(prompt: str) -> bytes:
    """Generate a JPEG using FLUX.1 schnell and the same Workers AI token."""
    api_key, base_url = _configuration()
    run_url = f"{base_url.removesuffix('/v1')}/run/{IMAGE_MODEL}"
    response = requests.post(
        run_url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"prompt": prompt, "steps": 4},
        timeout=(10, 180),
    )
    if not response.ok:
        raise RuntimeError(
            f"Cloudflare 配图生成失败（HTTP {response.status_code}）。"
            "请检查 API Token 的 Workers AI 权限和每日 Neuron 额度。"
        )
    payload = response.json()
    if payload.get("success") is False:
        raise RuntimeError("Cloudflare 配图接口返回失败（success=false）。")
    encoded_image = (payload.get("result") or {}).get("image")
    if not encoded_image:
        raise RuntimeError("Cloudflare 未返回生成的图片。")
    image_bytes = base64.b64decode(encoded_image, validate=True)
    if not image_bytes:
        raise RuntimeError("Cloudflare 返回的图片内容为空。")
    return image_bytes
