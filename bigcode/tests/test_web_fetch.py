from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

from bigcode.tools.base import ToolExecutionContext
from bigcode.tools.permissions import ToolPermissionContext
from bigcode.tools.read_file_state import ReadFileState
from bigcode.tools.web_fetch.WebFetch import WebFetchInput, WebFetchTool


class FakeResponse:
    def __init__(self, *, text: str, content_type: str, url: str = "https://example.com/page", status_code: int = 200) -> None:
        self.text = text
        self.headers = {"content-type": content_type}
        self.url = url
        self.status_code = status_code


class FakeAsyncClient:
    response: FakeResponse

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, *args: Any, **kwargs: Any) -> FakeResponse:
        return self.response


class WebFetchTests(unittest.TestCase):
    def test_html_response_is_returned_as_markdown(self) -> None:
        FakeAsyncClient.response = FakeResponse(
            text="<html><body><h1>Title</h1><p>Hello <a href=\"/x\">link</a></p><script>ignore()</script></body></html>",
            content_type="text/html; charset=utf-8",
        )

        with tempfile.TemporaryDirectory() as td, patch("bigcode.tools.web_fetch.WebFetch.httpx.AsyncClient", FakeAsyncClient):
            result = asyncio.run(WebFetchTool().call(WebFetchInput(url="https://example.com/page"), _make_context(Path(td))))

        self.assertEqual(result.data["content_format"], "markdown")
        self.assertEqual(result.data["content_type"], "text/html; charset=utf-8")
        self.assertIn("Title", result.data["text"])
        self.assertIn("Hello", result.data["text"])
        self.assertIn("link", result.data["text"])
        self.assertNotIn("<h1>", result.data["text"])
        self.assertNotIn("ignore()", result.data["text"])

    def test_plain_text_response_stays_plain_text(self) -> None:
        FakeAsyncClient.response = FakeResponse(text="plain body", content_type="text/plain")

        with tempfile.TemporaryDirectory() as td, patch("bigcode.tools.web_fetch.WebFetch.httpx.AsyncClient", FakeAsyncClient):
            result = asyncio.run(WebFetchTool().call(WebFetchInput(url="https://example.com/page"), _make_context(Path(td))))

        self.assertEqual(result.data["content_format"], "text")
        self.assertEqual(result.data["text"], "plain body")


def _make_context(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        cwd=root,
        workspace_roots=[root],
        permission_context=ToolPermissionContext(mode="default", should_avoid_permission_prompts=True),
        read_file_state=ReadFileState(),
        abort_event=Event(),
        session_id="sess",
        is_non_interactive_session=True,
    )
