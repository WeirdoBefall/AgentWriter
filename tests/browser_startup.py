"""Run with: python tests/browser_startup.py (requires Playwright and Chrome)."""

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://agentwriter.test"


async def check_startup():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page()
            submissions = []
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            blocked_cdn = asyncio.Event()

            async def route(request_route):
                url = request_route.request.url
                if url.startswith("https://cdn.jsdelivr.net/"):
                    await blocked_cdn.wait()
                    return
                if not url.startswith(BASE_URL):
                    await request_route.abort()
                    return
                path = url.removeprefix(BASE_URL).split("?", 1)[0]
                if path == "/":
                    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
                    html = html.replace("{{ page_title }}", "AgentWriter")
                    html = re.sub(r"{{ url_for\('static', path='([^']+)'\) }}", r"/static/\1", html)
                    await request_route.fulfill(body=html, content_type="text/html; charset=utf-8")
                elif path.startswith("/static/"):
                    file = ROOT / path.lstrip("/")
                    content_type = "text/javascript" if file.suffix == ".js" else "text/css" if file.suffix == ".css" else "image/svg+xml"
                    await request_route.fulfill(body=file.read_bytes(), content_type=content_type)
                elif path == "/api/health":
                    await request_route.fulfill(json={"status": "ok"})
                elif path == "/api/run":
                    submissions.append(request_route.request.post_data_json)
                    events = [
                        {"type": "run_started"},
                        {"type": "stage", "id": "router", "label": "分析写作需求", "status": "running"},
                        {"type": "final", "markdown": "# 中文文章\n\n测试正文。", "download_url": "/api/runs/test/download"},
                        {"type": "done"},
                    ]
                    body = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
                    await request_route.fulfill(body=body, content_type="text/event-stream; charset=utf-8")
                else:
                    await request_route.fulfill(status=404)

            await page.route("**/*", route)
            await page.goto(BASE_URL, wait_until="commit")
            await page.locator("#topicInput").fill("二分查找")
            await page.locator("#runButton").evaluate("button => button.click()")
            await page.wait_for_timeout(1000)
            assert submissions == [{"topic": "二分查找"}], (
                "点击后没有发送写作请求；CDN 阻塞时页面初始化失败。", submissions, errors,
            )
            await page.wait_for_function("document.querySelector('#runBadge').textContent === '已完成'", timeout=5000)
            assert await page.locator("#markdownOutput").text_content() == "# 中文文章\n\n测试正文。"
            assert await page.locator("#copyButton").is_enabled()
            assert not errors, errors
            print("PASS: CDN 不响应时，按钮仍提交主题并显示进度和最终文章。")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(check_startup())
