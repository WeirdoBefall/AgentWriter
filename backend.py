from __future__ import annotations

import operator
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import TypedDict, List, Optional, Literal, Annotated

from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_community.tools.tavily_search import TavilySearchResults

from cloudflare_models import BASE_DIR, create_chat_model, structured_output, generate_image_bytes
import psycopg
from psycopg.rows import dict_row
from langgraph.checkpoint.postgres import PostgresSaver



def get_database_url():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError(
            "缺少 DATABASE_URL，请在 .env 中配置 PostgreSQL 数据库连接地址。"
        )

    if "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{separator}sslmode=require"

    return database_url




# -----------------------------
# 1) Schemas
# -----------------------------
class Task(BaseModel):
    id: int
    title: str

    goal: str = Field(
        ...,
        description="用一句中文说明读者学完本节后能理解或完成什么。",
    )
    bullets: List[str] = Field(
        ...,
        min_length=3,
        max_length=6,
        description="本节需涵盖的 3～6 个具体、不重复的中文要点。",
    )
    target_words: int = Field(..., description="本节目标字数，120～550 个中文字符，不含代码。")

    tags: List[str] = Field(default_factory=list)
    requires_research: bool = False
    requires_citations: bool = False
    requires_code: bool = False


class Plan(BaseModel):
    blog_title: str
    audience: str
    tone: str
    blog_kind: Literal["explainer", "tutorial", "news_roundup", "comparison", "system_design"] = "explainer"
    constraints: List[str] = Field(default_factory=list)
    tasks: List[Task] = Field(..., min_length=5, max_length=9)


class EvidenceItem(BaseModel):
    title: str
    url: str
    published_at: Optional[str] = None  # keep if Tavily provides; DO NOT rely on it
    snippet: Optional[str] = None
    source: Optional[str] = None


class RouterDecision(BaseModel):
    needs_research: bool
    mode: Literal["closed_book", "hybrid", "open_book"]
    queries: List[str] = Field(default_factory=list)


class EvidencePack(BaseModel):
    evidence: List[EvidenceItem] = Field(default_factory=list)


class ImageSpec(BaseModel):
    placeholder: str = Field(..., description="配图占位符，例如 [[IMAGE_1]]")
    filename: str = Field(..., description="只填写英文 JPEG 文件名，例如 qkv_flow.jpg")
    alt: str
    caption: str
    prompt: str = Field(..., description="发给配图模型的英文画面描述，最多 2048 字符。")


class GlobalImagePlan(BaseModel):
    md_with_placeholders: str
    images: List[ImageSpec] = Field(default_factory=list)




class State(TypedDict):
    topic: str

    # routing / research
    mode: str
    needs_research: bool
    queries: List[str]
    evidence: List[EvidenceItem]
    plan: Optional[Plan]

    # workers
    sections: Annotated[List[tuple[int, str]], operator.add]  # (task_id, section_md)

    # reducer/image
    merged_md: str
    md_with_placeholders: str
    image_specs: List[dict]

    final: str




llm = create_chat_model()



# -----------------------------
# 3) Router (decide upfront)
# -----------------------------
ROUTER_SYSTEM = """你是中文技术博客的需求分析模块。在规划文章前，判断是否需要联网检索。
模式：
- closed_book：基础知识、原理等不依赖最新事实的主题，needs_research=false。
- hybrid：基础知识为主，但需要最新工具、模型或示例，needs_research=true。
- open_book：近期资讯、排名、价格、政策等时效性主题，needs_research=true。
需要检索时，生成 3～10 条具体且高价值的查询。优先中文查询；寻找官方文档时可以使用英文。
用户提到“本周”“上周”“最新”等时间要求时，应反映在查询中。
保留 Schema 的英文字段名及枚举值。"""

def router_node(state: State) -> dict:
    
    topic = state["topic"]
    decider = structured_output(llm, RouterDecision)
    decision = decider.invoke(
        [
            SystemMessage(content=ROUTER_SYSTEM),
            HumanMessage(content=f"主题：{topic}"),
        ]
    )

    return {
        "needs_research": decision.needs_research,
        "mode": decision.mode,
        "queries": decision.queries,
    }

def route_next(state: State) -> str:
    return "research" if state["needs_research"] else "orchestrator"





# -----------------------------
# 4) Research (Tavily) 
# -----------------------------
def _tavily_search(query: str, max_results: int = 5) -> List[dict]:
    
    tool = TavilySearchResults(max_results=max_results)
    results = tool.invoke({"query": query})

    normalized: List[dict] = []
    for r in results or []:
        normalized.append(
            {
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "snippet": r.get("content") or r.get("snippet") or "",
                "published_at": r.get("published_date") or r.get("published_at"),
                "source": r.get("source"),
            }
        )
    return normalized


RESEARCH_SYSTEM = """你是中文技术文章的资料整理员。根据原始检索结果，生成去重的 EvidenceItem 列表。
只收录 URL 非空的条目，优先选择相关的官方文档、公司博客和可靠媒体。
title 和 snippet 使用简体中文；必要的产品名、论文名可保留原文，摘要简洁准确。
只有结果明确提供发布日期时，才保留 YYYY-MM-DD 格式的 published_at；否则设为 null，不要猜测。
按 URL 去重，保持 URL 和来源信息准确，不得编造事实。"""

def research_node(state: State) -> dict:

    # take the first 10 queries from state
    queries = (state.get("queries", []) or [])
    max_results = 6

    raw_results: List[dict] = []

    for q in queries:
        raw_results.extend(_tavily_search(q, max_results=max_results))

    if not raw_results:
        return {"evidence": []}

    extractor = structured_output(llm, EvidencePack)
    pack = extractor.invoke(
        [
            SystemMessage(content=RESEARCH_SYSTEM),
            HumanMessage(content=f"原始检索资料：\n{raw_results}"),
        ]
    )

    # Deduplicate by URL
    dedup = {}
    for e in pack.evidence:
        if e.url:
            dedup[e.url] = e

    return {"evidence": list(dedup.values())}





# -----------------------------
# 5) Orchestrator (Plan)
# -----------------------------
ORCH_SYSTEM = """你是资深中文技术作者，为开发者制定可以直接执行的博客大纲。
文章标题、目标读者、语气、约束、章节标题、目标、要点和标签都必须使用简体中文。
即使主题以英文输入，也必须规划为中文文章；产品名、代码标识符和必要术语可保留原文。
硬性要求：
- 创建 5～9 个适合主题和读者的章节。
- 每章包含一句话目标、3～6 个具体且不重复的要点、120～550 字的目标字数（按中文字符计，不含代码）。
- 要点应可落实为实现、对比、测量、验证或排错。
- 全文至少覆盖以下两项：最小可运行代码、边界与故障、性能或成本、安全或隐私、调试与可观测性。
资料使用规则：
- closed_book：围绕稳定知识展开，不依赖近期信息。
- hybrid：用给定资料支撑最新示例，相应章节设置 requires_research=true 和 requires_citations=true。
- open_book：blog_kind 必须为 news_roundup，聚焦事件及影响；除非用户明确要求，不改写成教程。
- 资料不足时明确写“参考资料不足”，只安排有依据的内容。
严格遵循 Plan Schema；保留英文字段名和 blog_kind 等枚举值，其余面向读者的文字用简体中文。"""

def orchestrator_node(state: State) -> dict:
    planner = structured_output(llm, Plan)

    evidence = state.get("evidence", [])
    mode = state.get("mode", "closed_book")

    plan = planner.invoke(
        [
            SystemMessage(content=ORCH_SYSTEM),
            HumanMessage(
                content=(
                    f"主题：{state['topic']}\n"
                    f"写作模式：{mode}\n\n"
                    f"参考资料（近期事实只能依据这些资料，可能为空）：\n"
                    f"{[e.model_dump() for e in evidence][:16]}"
                )
            ),
        ]
    )

    return {"plan": plan}

# -----------------------------
# 6) Fanout
# -----------------------------
def fanout(state: State):
    return [
        Send(
            "worker",
            {
                "task": task.model_dump(),
                "topic": state["topic"],
                "mode": state["mode"],
                "plan": state["plan"].model_dump(),
                "evidence": [e.model_dump() for e in state.get("evidence", [])],
            },
        )
        for task in state["plan"].tasks
    ]






# -----------------------------
# 7) Worker (write one section)
# -----------------------------
WORKER_SYSTEM = """你是资深中文技术作者。只输出一个章节的 Markdown 内容。
语言与格式：
- 标题、正文、列表、表格、说明、代码注释和引用标签必须使用简体中文，即使输入主题是英文。
- 产品名、API 名、代码标识符、URL 和必要的英文术语保留原样，首次出现的术语可附中文解释。
- 严格依照目标，依次覆盖全部要点，不跳过也不随意合并。
- 字数按中文字符计（不含代码），接近目标字数，允许上下浮动 15%。
- 以“## 章节标题”开头，不输出文章一级标题、前言式对话或额外说明。
范围与事实：
- blog_kind 为 news_roundup 时，聚焦事件与影响；没有明确要求时，不扩展成爬虫、RSS 或自动化教程。
- mode 为 open_book 时，具体事件、公司、模型、融资、政策等事实必须有给定资料支持。
- 需要引用时，紧随事实附上 Markdown 链接：[来源](URL)。只允许使用给定资料中的 URL。
- 没有依据时写“未在所提供的资料中找到相关信息。”，不要编造。
- requires_citations=true 时，外部事实均需引用。稳定的原理推导可按要求自行阐述。
requires_code=true 时，必须包含与要点相关的最小正确代码示例，代码注释与解释使用中文。
使用短段落，适量列表和代码块，表达准确、自然，避免营销话术。"""

def worker_node(payload: dict) -> dict:
    
    task = Task(**payload["task"])
    plan = Plan(**payload["plan"])
    evidence = [EvidenceItem(**e) for e in payload.get("evidence", [])]
    topic = payload["topic"]
    mode = payload.get("mode", "closed_book")

    bullets_text = "\n- " + "\n- ".join(task.bullets)

    evidence_text = ""
    if evidence:
        evidence_text = "\n".join(
            f"- {e.title} | {e.url} | {e.published_at or '日期未知'}".strip()
            for e in evidence[:20]
        )

    section_md = llm.invoke(
        [
            SystemMessage(content=WORKER_SYSTEM),
            HumanMessage(
                content=(
                    f"文章标题：{plan.blog_title}\n"
                    f"目标读者：{plan.audience}\n"
                    f"写作风格：{plan.tone}\n"
                    f"文章类型：{plan.blog_kind}\n"
                    f"写作约束：{plan.constraints}\n"
                    f"主题：{topic}\n"
                    f"写作模式：{mode}\n\n"
                    f"章节标题：{task.title}\n"
                    f"本节目标：{task.goal}\n"
                    f"目标字数（中文字符，不含代码）：{task.target_words}\n"
                    f"标签：{task.tags}\n"
                    f"requires_research: {task.requires_research}\n"
                    f"requires_citations: {task.requires_citations}\n"
                    f"requires_code: {task.requires_code}\n"
                    f"内容要点：{bullets_text}\n\n"
                    f"参考资料（引用仅可使用以下 URL）：\n{evidence_text}\n"
                )
            ),
        ]
    ).content.strip()

    return {"sections": [(task.id, section_md)]}






# ============================================================
# 8) ReducerWithImages (subgraph)
#    merge_content -> decide_images -> generate_and_place_images
# ============================================================
def merge_content(state: State) -> dict:

    plan = state["plan"]

    ordered_sections = [md for _, md in sorted(state["sections"], key=lambda x: x[0])]
    body = "\n\n".join(ordered_sections).strip()
    merged_md = f"# {plan.blog_title}\n\n{body}\n"
    return {"merged_md": merged_md}


DECIDE_IMAGES_SYSTEM = """你是中文技术博客编辑，判断文章是否需要辅助理解的配图。
- 最多 3 张，只生成有解释价值的流程图、原理图等，避免装饰性图片。
- 保持全文为简体中文，保留原文标题、正文与引用，不翻译成英文，也不删改内容。
- 在适当位置插入准确的占位符 [[IMAGE_1]]、[[IMAGE_2]]、[[IMAGE_3]]。
- 不需要配图时，md_with_placeholders 必须与原文一致，images=[]。
- alt 和 caption 使用简体中文。filename 使用简短的英文 JPEG 文件名，以 .jpg 结尾。
- prompt 使用英文，最多 2048 字符，以便 FLUX.1 schnell 理解画面结构；优先无文字示意图，用中文图注解释。
- 不依赖模型在图内绘制中文文字；使用默认图片尺寸。
严格返回 GlobalImagePlan，Schema 字段名保持英文。"""

def decide_images(state: State) -> dict:
    
    planner = structured_output(llm, GlobalImagePlan)
    merged_md = state["merged_md"]
    plan = state["plan"]
    assert plan is not None

    image_plan = planner.invoke(
        [
            SystemMessage(content=DECIDE_IMAGES_SYSTEM),
            HumanMessage(
                content=(
                    f"文章类型：{plan.blog_kind}\n"
                    f"主题：{state['topic']}\n\n"
                    "插入配图占位符，并制定配图提示词。\n\n"
                    f"{merged_md}"
                )
            ),
        ]
    )

    return {
        "md_with_placeholders": image_plan.md_with_placeholders,
        "image_specs": [img.model_dump() for img in image_plan.images],
    }


def generate_and_place_images(state: State) -> dict:

    plan = state["plan"]
    assert plan is not None

    md = state.get("md_with_placeholders") or state["merged_md"]
    image_specs = state.get("image_specs", []) or []
    outputs_dir = BASE_DIR / "outputs"
    outputs_dir.mkdir(exist_ok=True)

    # If no images requested, just write merged markdown
    if not image_specs:
        filename = f"{plan.blog_title}.md"
        (outputs_dir / filename).write_text(md, encoding="utf-8")
        return {"final": md}

    images_dir = BASE_DIR / "images"
    images_dir.mkdir(exist_ok=True)

    for spec in image_specs:
        placeholder = spec["placeholder"]
        filename = Path(spec["filename"]).with_suffix(".jpg").name
        out_path = images_dir / filename

        # generate only if needed
        if not out_path.exists():
            try:
                img_bytes = generate_image_bytes(spec["prompt"])
                out_path.write_bytes(img_bytes)
            except Exception as e:
                # graceful fallback: keep doc usable
                prompt_block = (
                    f"> **[配图生成失败]** {spec.get('caption','')}\n>\n"
                    f"> **图片说明：** {spec.get('alt','')}\n>\n"
                    f"> **配图提示词：** {spec.get('prompt','')}\n>\n"
                    f"> **错误信息：** {e}\n"
                )
                md = md.replace(placeholder, prompt_block)
                continue

        img_md = f"![{spec['alt']}](images/{filename})\n*{spec['caption']}*"
        md = md.replace(placeholder, img_md)

    filename = f"{plan.blog_title}.md"
    (outputs_dir / filename).write_text(md, encoding="utf-8")
    return {"final": md}




# build reducer subgraph
reducer_graph = StateGraph(State)
reducer_graph.add_node("merge_content", merge_content)
reducer_graph.add_node("decide_images", decide_images)
reducer_graph.add_node("generate_and_place_images", generate_and_place_images)
reducer_graph.add_edge(START, "merge_content")
reducer_graph.add_edge("merge_content", "decide_images")
reducer_graph.add_edge("decide_images", "generate_and_place_images")
reducer_graph.add_edge("generate_and_place_images", END)
reducer_subgraph = reducer_graph.compile()

reducer_subgraph



# -----------------------------
# 9) Build main graph
# -----------------------------
g = StateGraph(State)
g.add_node("router", router_node)
g.add_node("research", research_node)
g.add_node("orchestrator", orchestrator_node)
g.add_node("worker", worker_node)
g.add_node("reducer", reducer_subgraph)

g.add_edge(START, "router")
g.add_conditional_edges("router", route_next, {"research": "research", "orchestrator": "orchestrator"})
g.add_edge("research", "orchestrator")

g.add_conditional_edges("orchestrator", fanout, ["worker"])
g.add_edge("worker", "reducer")
g.add_edge("reducer", END)



# =========================
# PostgreSQL Checkpointer
# =========================
DATABASE_URL = get_database_url()

_conn = psycopg.connect(
    DATABASE_URL,
    autocommit=True,
    row_factory=dict_row
)

checkpointer = PostgresSaver(_conn)
checkpointer.setup()



app = g.compile(checkpointer=checkpointer)
app
