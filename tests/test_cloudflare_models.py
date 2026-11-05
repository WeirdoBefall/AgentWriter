import base64
import json
import os
import unittest
from unittest.mock import Mock, patch

import httpx
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

import cloudflare_models as models


class Section(BaseModel):
    title: str


class Outline(BaseModel):
    sections: list[Section]


class CloudflareModelsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "CLOUDFLARE_API_KEY": "test-token",
            "CLOUDFLARE_BASEURL": "https://api.cloudflare.com/client/v4/accounts/test/ai/v1/",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_chat_and_schema_use_cloudflare_wire_format(self):
        def respond(request):
            self.assertEqual(str(request.url),
                             "https://api.cloudflare.com/client/v4/accounts/test/ai/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-token")
            body = json.loads(request.content)
            self.assertEqual(body["model"], models.TEXT_MODEL)
            self.assertEqual(body["response_format"], {
                "type": "json_schema", "json_schema": Outline.model_json_schema(),
            })
            self.assertIn('"sections"', body["messages"][0]["content"])
            self.assertIn('"Section"', body["messages"][0]["content"])
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 0,
                "model": models.TEXT_MODEL,
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": '{"sections":[{"title":"Introduction"}]}',
                }}],
            })

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with patch("cloudflare_models.ChatOpenAI", side_effect=lambda **kwargs:
                       ChatOpenAI(http_client=client, **kwargs)):
                llm = models.create_chat_model()
            result = models.structured_output(llm, Outline).invoke([HumanMessage("Plan an article")])
        self.assertEqual(result, Outline(sections=[Section(title="Introduction")]))

    @patch("cloudflare_models.requests.post")
    def test_flux_url_payload_and_base64_decoding(self, post):
        jpeg = b"\xff\xd8\xfftest-image"
        post.return_value = Mock(ok=True)
        post.return_value.json.return_value = {
            "success": True, "result": {"image": base64.b64encode(jpeg).decode()},
        }
        self.assertEqual(models.generate_image_bytes("A diagram"), jpeg)
        post.assert_called_once_with(
            "https://api.cloudflare.com/client/v4/accounts/test/ai/run/" + models.IMAGE_MODEL,
            headers={"Authorization": "Bearer test-token"},
            json={"prompt": "A diagram", "steps": 4}, timeout=(10, 180),
        )

    @patch("cloudflare_models.requests.post")
    def test_quota_error_is_reported_without_secret_response_body(self, post):
        post.return_value = Mock(ok=False, status_code=429)
        with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
            models.generate_image_bytes("A diagram")

    @patch("cloudflare_models.requests.post")
    def test_missing_image_is_an_error(self, post):
        post.return_value = Mock(ok=True)
        post.return_value.json.return_value = {"success": True, "result": {}}
        with self.assertRaisesRegex(RuntimeError, "未返回生成的图片"):
            models.generate_image_bytes("A diagram")

    def test_missing_configuration_fails_early(self):
        with patch.dict(os.environ, {"CLOUDFLARE_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "CLOUDFLARE_API_KEY"):
                models.create_chat_model()


if __name__ == "__main__":
    unittest.main()
