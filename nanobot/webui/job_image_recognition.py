"""One bounded, tool-free vision request using an existing immutable LLM runtime."""

from __future__ import annotations

import asyncio
import base64
import io
import secrets
from collections.abc import Callable
from typing import Annotated

from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import StringConstraints, ValidationError

from nanobot.career.models import JobImageError, JobImageReading
from nanobot.providers.base import ProviderCallContext
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.utils.prompt_templates import render_template


class JobVisionError(Exception):
    def __init__(self, code: JobImageError) -> None:
        super().__init__(code)
        self.code: JobImageError = code


class VisionReading(JobImageReading):
    vision_code: Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


def image_challenge(code: str) -> tuple[str, str]:
    image = Image.new("RGB", (320, 90), "white")
    ImageDraw.Draw(image).text((20, 20), code, fill="black", font=ImageFont.load_default(size=42))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return code, "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _challenge() -> tuple[str, str]:
    return image_challenge(str(secrets.randbelow(900_000) + 100_000))


def _model_image(content: bytes) -> str:
    # Preserve the business original; send an oriented, metadata-free PNG copy
    # so the configured provider sees the same supported type for JPG and WEBP.
    with Image.open(io.BytesIO(content)) as original:
        image = ImageOps.exif_transpose(original)
        image.thumbnail((2400, 3600))
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA")
        image.info.clear()
        output = io.BytesIO()
        image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


class JobImageRecognizer:
    def __init__(self, resolve_runtime: Callable[[], LLMRuntime]) -> None:
        self.resolve_runtime = resolve_runtime

    async def recognize(self, runtime: LLMRuntime, content: bytes) -> JobImageReading:
        code, challenge = _challenge()
        image_url = await asyncio.to_thread(_model_image, content)
        response = await runtime.provider.chat_with_context(
            provider_context=ProviderCallContext(context_window_tokens=runtime.context_window_tokens),
            messages=[
                {"role": "system", "content": render_template("agent/career_job_image.md", schema=VisionReading.model_json_schema())},
                {"role": "user", "content": [
                    {"type": "text", "text": "第一张图是岗位截图，第二张图仅用于读图校验。提取第一张图的岗位原文及字段，把第二张图的六位数字放入 vision_code。只返回 JSON。"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "image_url", "image_url": {"url": challenge}},
                ]},
            ],
            tools=None, model=runtime.model, max_tokens=min(runtime.generation.max_tokens, 8192),
            temperature=0, reasoning_effort=runtime.generation.reasoning_effort,
        )
        if response.finish_reason == "error":
            detail = (response.content or "").lower()
            unsupported = (response.error_status_code in {400, 422}
                           and any(word in detail for word in ("image", "vision", "图像", "图片"))
                           and any(word in detail for word in ("unsupported", "not support", "not allowed", "不支持")))
            raise JobVisionError("vision_unavailable" if unsupported else "timeout" if response.error_kind == "timeout" else "recognition_failed")
        if response.tool_calls or response.finish_reason == "length" or len(response.content or "") > 100_000:
            raise JobVisionError("invalid_result")
        payload = (response.content or "").strip()
        if payload.startswith("```json\n") and payload.endswith("```"):
            payload = payload[len("```json\n"):-3].strip()
        try:
            reading = VisionReading.model_validate_json(payload)
        except ValidationError as error:
            missing_vision = any(issue["loc"] == ("vision_code",) for issue in error.errors(include_input=False, include_url=False))
            raise JobVisionError("vision_unverified" if missing_vision else "invalid_result") from error
        if reading.vision_code != code:
            raise JobVisionError("vision_unverified")
        return JobImageReading.model_validate(reading.model_dump(exclude={"vision_code"}))
