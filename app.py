from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Generator

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import uvicorn
# ---------------------------------------------------------
# Import the existing compiled LangGraph workflow.
#
# backend.py remains completely unchanged.
# backend.app is the compiled LangGraph workflow.
# ---------------------------------------------------------
from backend import app as workflow


# ---------------------------------------------------------
# Paths
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
IMAGES_DIR = BASE_DIR / "images"
OUTPUTS_DIR = BASE_DIR / "outputs"

TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("langgraph-fastapi")


# ---------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------
app = FastAPI(
    title="AgentWriter 中文博客助手",
    description="基于 Cloudflare 与 LangGraph 的中文技术博客写作工作台。",
    version="1.0.0",
)


# ---------------------------------------------------------
# Static files
# ---------------------------------------------------------
app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static",
)

# backend.py saves generated images inside images/
app.mount(
    "/images",
    StaticFiles(directory=IMAGES_DIR),
    name="images",
)


# ---------------------------------------------------------
# Templates
# ---------------------------------------------------------
templates = Jinja2Templates(
    directory=TEMPLATES_DIR,
)


# ---------------------------------------------------------
# Request schema
# ---------------------------------------------------------
class AgentRunRequest(BaseModel):
    topic: str = Field(
        ...,
        min_length=2,
        max_length=1000,
        description="要撰写的中文技术博客主题。",
    )


# ---------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------
def make_serializable(value: Any) -> Any:
    """
    Recursively convert Pydantic models and other values
    into JSON-compatible Python values.
    """

    if hasattr(value, "model_dump"):
        return make_serializable(value.model_dump())

    if hasattr(value, "dict"):
        return make_serializable(value.dict())

    if isinstance(value, dict):
        return {
            str(key): make_serializable(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            make_serializable(item)
            for item in value
        ]

    return value


def create_sse_event(
    payload: dict[str, Any],
    event_name: str | None = None,
) -> str:
    """
    Convert a dictionary to a Server-Sent Event message.
    """

    encoded_payload = json.dumps(
        jsonable_encoder(payload),
        ensure_ascii=False,
    )

    lines: list[str] = []

    if event_name:
        lines.append(f"event: {event_name}")

    lines.append(f"data: {encoded_payload}")

    return "\n".join(lines) + "\n\n"


def normalize_stream_chunk(
    chunk: Any,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """
    Normalize LangGraph streaming chunks.

    With subgraphs=True, LangGraph commonly returns:

        (namespace, update)

    Example:

        (
            ("reducer:<task-id>",),
            {"merge_content": {...}}
        )

    Root graph updates may also be returned directly
    as dictionaries depending on the LangGraph version.
    """

    if (
        isinstance(chunk, tuple)
        and len(chunk) == 2
        and isinstance(chunk[1], dict)
    ):
        raw_namespace = chunk[0] or ()

        namespace = tuple(
            str(item)
            for item in raw_namespace
        )

        return namespace, chunk[1]

    if isinstance(chunk, dict):
        return (), chunk

    return (), {}


def get_plan_task_map(
    plan: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    """
    Create a task lookup using each task ID.
    """

    task_map: dict[int, dict[str, Any]] = {}

    tasks = plan.get("tasks", [])

    if not isinstance(tasks, list):
        return task_map

    for task in tasks:
        if not isinstance(task, dict):
            continue

        try:
            task_id = int(task["id"])
        except (KeyError, TypeError, ValueError):
            continue

        task_map[task_id] = task

    return task_map


def save_final_markdown(
    run_id: str,
    markdown: str,
) -> Path:
    """
    Save a predictable copy of the generated Markdown.

    This does not change backend.py. The backend can continue
    saving its original title-based Markdown file.
    """

    run_directory = OUTPUTS_DIR / run_id
    run_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_file = run_directory / "blog.md"

    output_file.write_text(
        markdown,
        encoding="utf-8",
    )

    return output_file


# ---------------------------------------------------------
# LangGraph streaming
# ---------------------------------------------------------
def stream_workflow(
    topic: str,
    run_id: str,
) -> Generator[str, None, None]:
    """
    Run the existing LangGraph workflow and stream
    observable execution updates to the browser.

    This exposes:
    - node status
    - routing decision
    - research queries and source count
    - structured article plan
    - completed sections
    - image-processing status
    - final Markdown

    It does not expose private model reasoning.
    """

    config = {
        "configurable": {
            "thread_id": run_id,
        }
    }

    workflow_input = {
        "topic": topic,
        "sections": [],
    }

    task_map: dict[int, dict[str, Any]] = {}
    completed_task_ids: set[int] = set()

    final_markdown = ""
    workers_completed_event_sent = False
    reducer_started_event_sent = False

    yield create_sse_event(
        {
            "type": "run_started",
            "run_id": run_id,
            "topic": topic,
        }
    )

    yield create_sse_event(
        {
            "type": "stage",
            "id": "router",
            "label": "分析写作需求",
            "status": "running",
            "detail": (
                "正在判断主题是否需要联网检索最新资料。"
            ),
        }
    )

    try:
        stream = workflow.stream(
            workflow_input,
            config=config,
            stream_mode="updates",
            subgraphs=True,
        )

        for raw_chunk in stream:
            namespace, updates = normalize_stream_chunk(
                raw_chunk
            )

            if not updates:
                continue

            for node_name, raw_node_update in updates.items():
                node_update = make_serializable(
                    raw_node_update
                )

                if not isinstance(node_update, dict):
                    node_update = {}

                # =================================================
                # Router
                # =================================================
                if node_name == "router":
                    mode = str(
                        node_update.get(
                            "mode",
                            "closed_book",
                        )
                    )

                    mode_label = {
                        "closed_book": "基础知识",
                        "hybrid": "资料辅助",
                        "open_book": "时效资讯",
                    }.get(mode, "自动")

                    needs_research = bool(
                        node_update.get(
                            "needs_research",
                            False,
                        )
                    )

                    queries = node_update.get(
                        "queries",
                        [],
                    )

                    yield create_sse_event(
                        {
                            "type": "routing",
                            "mode": mode,
                            "needs_research": needs_research,
                            "queries": queries,
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "stage",
                            "id": "router",
                            "label": "分析写作需求",
                            "status": "completed",
                            "detail": (
                                f"已选择{mode_label}模式。"
                            ),
                        }
                    )

                    if needs_research:
                        yield create_sse_event(
                            {
                                "type": "stage",
                                "id": "research",
                                "label": "检索权威资料",
                                "status": "running",
                                "detail": (
                                    "正在检索网络资料，并整理去重后的参考来源。"
                                ),
                            }
                        )

                    else:
                        yield create_sse_event(
                            {
                                "type": "stage",
                                "id": "orchestrator",
                                "label": "制定文章大纲",
                                "status": "running",
                                "detail": (
                                    "正在规划文章结构、章节目标和写作任务。"
                                ),
                            }
                        )

                # =================================================
                # Research
                # =================================================
                elif node_name == "research":
                    evidence = node_update.get(
                        "evidence",
                        [],
                    )

                    if not isinstance(evidence, list):
                        evidence = []

                    yield create_sse_event(
                        {
                            "type": "research_complete",
                            "count": len(evidence),
                            "evidence": evidence[:12],
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "stage",
                            "id": "research",
                            "label": "检索权威资料",
                            "status": "completed",
                            "detail": (
                                f"已整理 {len(evidence)} 条去重后的参考来源。"
                            ),
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "stage",
                            "id": "orchestrator",
                            "label": "制定文章大纲",
                            "status": "running",
                            "detail": (
                                "正在制定章节、写作目标、内容要点和目标字数。"
                            ),
                        }
                    )

                # =================================================
                # Orchestrator
                # =================================================
                elif node_name == "orchestrator":
                    plan = node_update.get(
                        "plan",
                        {},
                    )

                    if not isinstance(plan, dict):
                        plan = {}

                    task_map = get_plan_task_map(plan)

                    yield create_sse_event(
                        {
                            "type": "plan",
                            "plan": plan,
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "stage",
                            "id": "orchestrator",
                            "label": "制定文章大纲",
                            "status": "completed",
                            "detail": (
                                f"已规划 {len(task_map)} 个文章章节。"
                            ),
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "stage",
                            "id": "workers",
                            "label": "撰写文章章节",
                            "status": "running",
                            "detail": (
                                "正在并行撰写各个章节。"
                            ),
                        }
                    )

                # =================================================
                # Workers
                # =================================================
                elif node_name == "worker":
                    sections = node_update.get(
                        "sections",
                        [],
                    )

                    if not isinstance(sections, list):
                        sections = []

                    for section in sections:
                        if not isinstance(
                            section,
                            (list, tuple),
                        ):
                            continue

                        if len(section) != 2:
                            continue

                        raw_task_id, section_markdown = section

                        try:
                            task_id = int(raw_task_id)
                        except (TypeError, ValueError):
                            continue

                        # A parallel worker update should be sent once.
                        if task_id in completed_task_ids:
                            continue

                        completed_task_ids.add(task_id)

                        task_information = task_map.get(
                            task_id,
                            {},
                        )

                        title = task_information.get(
                            "title",
                            f"第 {task_id} 节",
                        )

                        yield create_sse_event(
                            {
                                "type": "section_complete",
                                "task_id": task_id,
                                "title": title,
                                "markdown": str(section_markdown),
                                "completed": len(
                                    completed_task_ids
                                ),
                                "total": len(task_map),
                            }
                        )

                    if (
                        task_map
                        and len(completed_task_ids) >= len(task_map)
                        and not workers_completed_event_sent
                    ):
                        workers_completed_event_sent = True

                        yield create_sse_event(
                            {
                                "type": "stage",
                                "id": "workers",
                                "label": "撰写文章章节",
                                "status": "completed",
                                "detail": (
                                    f"全部 {len(task_map)} 个章节已完成。"
                                ),
                            }
                        )

                        yield create_sse_event(
                            {
                                "type": "stage",
                                "id": "reducer",
                                "label": "整合最终文章",
                                "status": "running",
                                "detail": (
                                    "正在合并章节并规划博客配图。"
                                ),
                            }
                        )

                        reducer_started_event_sent = True

                # =================================================
                # Reducer subgraph: merge
                # =================================================
                elif node_name == "merge_content":
                    if not reducer_started_event_sent:
                        yield create_sse_event(
                            {
                                "type": "stage",
                                "id": "reducer",
                                "label": "整合最终文章",
                                "status": "running",
                                "detail": (
                                    "正在合并章节并规划博客配图。"
                                ),
                            }
                        )

                        reducer_started_event_sent = True

                    yield create_sse_event(
                        {
                            "type": "substage",
                            "id": "merge_content",
                            "label": "已合并全部章节",
                            "status": "completed",
                            "namespace": list(namespace),
                        }
                    )

                # =================================================
                # Reducer subgraph: image plan
                # =================================================
                elif node_name == "decide_images":
                    image_specs = node_update.get(
                        "image_specs",
                        [],
                    )

                    if not isinstance(image_specs, list):
                        image_specs = []

                    yield create_sse_event(
                        {
                            "type": "images_planned",
                            "count": len(image_specs),
                            "images": image_specs,
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "substage",
                            "id": "decide_images",
                            "label": (
                                f"已规划 {len(image_specs)} 张博客配图"
                            ),
                            "status": "completed",
                            "namespace": list(namespace),
                        }
                    )

                # =================================================
                # Reducer subgraph: image generation and final text
                # =================================================
                elif node_name == "generate_and_place_images":
                    generated_final = node_update.get(
                        "final"
                    )

                    if generated_final:
                        final_markdown = str(
                            generated_final
                        )

                    yield create_sse_event(
                        {
                            "type": "substage",
                            "id": "generate_images",
                            "label": "已生成并插入配图",
                            "status": "completed",
                            "namespace": list(namespace),
                        }
                    )

                # =================================================
                # Root reducer update
                # =================================================
                elif node_name == "reducer":
                    generated_final = node_update.get(
                        "final"
                    )

                    if generated_final:
                        final_markdown = str(
                            generated_final
                        )

        # -----------------------------------------------------
        # Retrieve the final checkpoint when the root reducer
        # update did not contain the complete final output.
        # -----------------------------------------------------
        if not final_markdown:
            snapshot = workflow.get_state(config)

            state_values = getattr(
                snapshot,
                "values",
                {},
            )

            if isinstance(state_values, dict):
                final_markdown = str(
                    state_values.get(
                        "final",
                        "",
                    )
                )

        if not final_markdown:
            raise RuntimeError(
                "写作流程已结束，但未返回完整的 Markdown 文章。"
            )

        save_final_markdown(
            run_id=run_id,
            markdown=final_markdown,
        )

        yield create_sse_event(
            {
                "type": "stage",
                "id": "reducer",
                "label": "整合最终文章",
                "status": "completed",
                "detail": "完整的中文 Markdown 文章已生成。",
            }
        )

        yield create_sse_event(
            {
                "type": "final",
                "run_id": run_id,
                "markdown": final_markdown,
                "download_url": (
                    f"/api/runs/{run_id}/download"
                ),
            }
        )

        yield create_sse_event(
            {
                "type": "done",
                "run_id": run_id,
            }
        )

    except GeneratorExit:
        logger.info(
            "Browser disconnected from run %s",
            run_id,
        )
        raise

    except Exception as error:
        logger.exception(
            "Workflow run %s failed",
            run_id,
        )

        yield create_sse_event(
            {
                "type": "error",
                "run_id": run_id,
                "message": f"生成文章失败：{error}",
            }
        )


# ---------------------------------------------------------
# Page endpoint
# ---------------------------------------------------------
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get(
    "/",
    response_class=HTMLResponse,
)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "page_title": "AgentWriter 中文博客助手",
        },
    )


# ---------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------
@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "workflow": "loaded",
    }


# ---------------------------------------------------------
# Agent execution endpoint
# ---------------------------------------------------------
@app.post("/api/run")
def run_agent(request_data: AgentRunRequest):
    topic = request_data.topic.strip()

    if len(topic) < 2:
        raise HTTPException(
            status_code=422,
            detail="请输入至少 2 个字的博客主题。",
        )

    run_id = uuid.uuid4().hex

    return StreamingResponse(
        stream_workflow(
            topic=topic,
            run_id=run_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------
# Markdown download endpoint
# ---------------------------------------------------------
@app.get("/api/runs/{run_id}/download")
def download_markdown(run_id: str):
    # Only allow safe run ID characters.
    safe_run_id = "".join(
        character
        for character in run_id
        if character.isalnum()
        or character in {"-", "_"}
    )

    if safe_run_id != run_id:
        raise HTTPException(
            status_code=400,
            detail="无效的任务编号。",
        )

    output_file = (
        OUTPUTS_DIR
        / safe_run_id
        / "blog.md"
    )

    if not output_file.is_file():
        raise HTTPException(
            status_code=404,
            detail="未找到生成的 Markdown 文章。",
        )

    return FileResponse(
        path=output_file,
        media_type="text/markdown",
        filename=f"generated-blog-{safe_run_id[:8]}.md",
    )




if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=True
    )
