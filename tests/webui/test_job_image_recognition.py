from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image, PngImagePlugin

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.generation import GenerationSettings
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.webui.job_image_recognition import JobImageRecognizer, JobVisionError, image_challenge


class VisionProvider(LLMProvider):
    def __init__(self, response: LLMResponse | None = None):
        super().__init__(provider_name="engineering-vision")
        self.response = response or LLMResponse(content=json.dumps({
            "vision_code": "246810", "readable": True, "text": "工程岗位 MODEL_JOB_A\n4天/周 6个月",
            "fields": {"name": {"value": "工程岗位 MODEL_JOB_A", "evidence": "工程岗位 MODEL_JOB_A"},
                       "days_per_week": {"value": 4, "evidence": "4天/周"},
                       "available_months": {"value": 6, "evidence": "6个月"}}, "warnings": [],
        }, ensure_ascii=False))
        self.calls = []

    def get_default_model(self):
        return "engineering-vision-model"

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.response


def screenshot(kind="png"):
    image = Image.new("RGB", (320, 160), "white")
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-test-metadata", "metadata must not be sent")
    image.save(output, format={"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}[kind], **({"pnginfo": metadata} if kind == "png" else {}))
    return output.getvalue()


@pytest.fixture(autouse=True)
def fixed_challenge(monkeypatch):
    monkeypatch.setattr("nanobot.webui.job_image_recognition._challenge", lambda: image_challenge("246810"))


@pytest.mark.parametrize("kind", ["png", "jpg", "webp"])
async def test_uses_real_image_blocks_and_frozen_runtime_without_forwarding_metadata_or_tools(kind):
    provider = VisionProvider()
    provider.generation = GenerationSettings(max_tokens=4096, reasoning_effort="low")
    runtime = LLMRuntime.capture(provider, "frozen-model", context_window_tokens=32_000)
    provider.generation = GenerationSettings(max_tokens=16_000, reasoning_effort="high")
    result = await JobImageRecognizer(lambda: runtime).recognize(runtime, screenshot(kind))
    assert result.readable and result.fields.days_per_week.value == 4
    messages, parameters = provider.calls[0]
    assert parameters["model"] == "frozen-model" and parameters["max_tokens"] == 4096 and parameters["reasoning_effort"] == "low"
    assert parameters["tools"] is None
    blocks = messages[-1]["content"]
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")
    raw = base64.b64decode(blocks[1]["image_url"]["url"].split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        assert image.format == "PNG" and "private-test-metadata" not in image.info
    assert blocks[2]["type"] == "image_url"
    assert "246810" not in blocks[0]["text"]


@pytest.mark.parametrize(("response", "expected"), [
    (LLMResponse(content='{"vision_code":"111111","readable":true,"text":"编造岗位"}'), "vision_unverified"),
    (LLMResponse(content='{"vision_code":"","readable":false,"text":""}'), "vision_unverified"),
    (LLMResponse(content="not valid JSON"), "invalid_result"),
    (LLMResponse(content="image input is not supported", finish_reason="error", error_status_code=400), "vision_unavailable"),
    (LLMResponse(content="", tool_calls=[ToolCallRequest("unsafe", "write_file", {"content": "untrusted image instructions"})]), "invalid_result"),
], ids=["wrong-image-proof", "cannot-see-images", "invalid-json", "text-only-service", "tool-call-not-executed"])
async def test_failed_or_unverified_model_output_never_becomes_a_recognized_job(response, expected):
    provider = VisionProvider(response)
    runtime = LLMRuntime.capture(provider, "engineering-model", context_window_tokens=32_000)
    with pytest.raises(JobVisionError) as error:
        await JobImageRecognizer(lambda: runtime).recognize(runtime, screenshot())
    assert error.value.code == expected
