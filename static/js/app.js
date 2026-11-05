const runForm = document.getElementById("runForm");
const topicInput = document.getElementById("topicInput");
const runButton = document.getElementById("runButton");
const cancelButton = document.getElementById("cancelButton");
const newRunButton = document.getElementById("newRunButton");

const timeline = document.getElementById("timeline");
const activityEmpty = document.getElementById("activityEmpty");

const planList = document.getElementById("planList");
const planEmpty = document.getElementById("planEmpty");
const planMeta = document.getElementById("planMeta");
const taskCount = document.getElementById("taskCount");

const progressRing = document.getElementById("progressRing");
const progressText = document.getElementById("progressText");
const runBadge = document.getElementById("runBadge");

const articlePreview = document.getElementById("articlePreview");
const markdownOutput = document.getElementById("markdownOutput");

const previewTab = document.getElementById("previewTab");
const markdownTab = document.getElementById("markdownTab");

const copyButton = document.getElementById("copyButton");
const downloadButton = document.getElementById("downloadButton");

const healthDot = document.getElementById("healthDot");
const healthText = document.getElementById("healthText");
const toast = document.getElementById("toast");

let abortController = null;
let finalMarkdown = "";
let totalTasks = 0;
let completedTasks = 0;

const stageProgress = {
    router: 10,
    research: 25,
    orchestrator: 35,
    workers: 75,
    reducer: 100,
};


function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


function showToast(message) {
    toast.textContent = message;
    toast.classList.add("show");

    window.setTimeout(() => {
        toast.classList.remove("show");
    }, 2200);
}


function updateProgress(value) {
    const progress = Math.max(
        0,
        Math.min(100, Math.round(value)),
    );

    progressRing.style.setProperty(
        "--progress",
        progress,
    );

    progressText.textContent = `${progress}%`;
}


function resetInterface() {
    finalMarkdown = "";
    totalTasks = 0;
    completedTasks = 0;

    timeline.innerHTML = "";
    planList.innerHTML = "";

    activityEmpty.hidden = false;
    planEmpty.hidden = false;

    timeline.appendChild(activityEmpty);
    planList.appendChild(planEmpty);

    planMeta.innerHTML = "";
    planMeta.hidden = true;

    taskCount.textContent = "0 个章节";

    articlePreview.innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">✎</div>
            <h3>你的文章将在这里呈现</h3>
            <p>
                所有章节完成后，即可预览、复制或下载完整的中文文章。
            </p>
        </div>
    `;

    markdownOutput.textContent = "";
    markdownOutput.hidden = true;
    articlePreview.hidden = false;

    previewTab.classList.add("active");
    markdownTab.classList.remove("active");

    copyButton.disabled = true;

    downloadButton.href = "#";
    downloadButton.classList.add("disabled");
    downloadButton.setAttribute(
        "aria-disabled",
        "true",
    );

    runBadge.textContent = "准备就绪";
    runBadge.className = "run-badge";

    updateProgress(0);
}


function setRunningState(isRunning) {
    topicInput.disabled = isRunning;
    runButton.disabled = isRunning;

    cancelButton.hidden = !isRunning;

    if (isRunning) {
        runBadge.textContent = "正在生成";
        runBadge.className = "run-badge running";
    }
}


function getStageElement(stageId) {
    return document.querySelector(
        `[data-stage-id="${stageId}"]`,
    );
}


function createStageElement(stageId) {
    const element = document.createElement("div");

    element.className = "timeline-item";
    element.dataset.stageId = stageId;

    element.innerHTML = `
        <div class="timeline-marker">
            <span></span>
        </div>

        <div class="timeline-content">
            <div class="timeline-title"></div>
            <div class="timeline-detail"></div>
        </div>
    `;

    timeline.appendChild(element);

    return element;
}


function updateStage(event) {
    activityEmpty.hidden = true;

    let element = getStageElement(event.id);

    if (!element) {
        element = createStageElement(event.id);
    }

    element.classList.remove(
        "running",
        "completed",
        "failed",
    );

    element.classList.add(
        event.status || "running",
    );

    const title = element.querySelector(
        ".timeline-title",
    );

    const detail = element.querySelector(
        ".timeline-detail",
    );

    title.textContent = event.label;
    detail.textContent = event.detail || "";

    if (
        event.status === "completed"
        && stageProgress[event.id] !== undefined
    ) {
        updateProgress(stageProgress[event.id]);
    }
}


function addSubstage(event) {
    activityEmpty.hidden = true;

    const element = document.createElement("div");

    element.className =
        "timeline-item substage completed";

    element.innerHTML = `
        <div class="timeline-marker">
            <span></span>
        </div>

        <div class="timeline-content">
            <div class="timeline-title">
                ${escapeHtml(event.label)}
            </div>

            <div class="timeline-detail">
                已完成
            </div>
        </div>
    `;

    timeline.appendChild(element);
}


function showRouting(event) {
    const modeLabels = {
        closed_book: "基础知识主题",
        hybrid: "资料辅助主题",
        open_book: "时效资讯主题",
    };

    const detail =
        modeLabels[event.mode]
        || event.mode;

    const element = getStageElement("router");

    if (element) {
        const detailElement = element.querySelector(
            ".timeline-detail",
        );

        detailElement.textContent =
            `${detail}，${
                event.needs_research
                    ? "需要联网检索"
                    : "无需联网检索"
            }。`;
    }

    if (
        Array.isArray(event.queries)
        && event.queries.length
    ) {
        const queriesElement = document.createElement(
            "div",
        );

        queriesElement.className = "query-list";

        queriesElement.innerHTML = event.queries
            .map(
                query => `
                    <span>
                        ${escapeHtml(query)}
                    </span>
                `,
            )
            .join("");

        if (element) {
            element
                .querySelector(".timeline-content")
                .appendChild(queriesElement);
        }
    }
}


function showResearch(event) {
    if (!event.evidence?.length) {
        return;
    }

    const stage = getStageElement("research");

    if (!stage) {
        return;
    }

    const sources = document.createElement("div");
    sources.className = "source-list";

    sources.innerHTML = event.evidence
        .slice(0, 5)
        .map(source => `
            <a
                href="${escapeHtml(source.url)}"
                target="_blank"
                rel="noopener noreferrer"
            >
                ${escapeHtml(
                    source.title || source.url,
                )}
            </a>
        `)
        .join("");

    stage
        .querySelector(".timeline-content")
        .appendChild(sources);
}


const blogKindLabels = {
    explainer: "原理讲解",
    tutorial: "实战教程",
    news_roundup: "资讯汇总",
    comparison: "对比分析",
    system_design: "系统设计",
};


function showPlan(plan) {
    planEmpty.hidden = true;
    planList.innerHTML = "";

    const tasks = plan.tasks || [];

    totalTasks = tasks.length;
    completedTasks = 0;

    taskCount.textContent =
        `${tasks.length} 个章节`;

    planMeta.hidden = false;

    planMeta.innerHTML = `
        <div>
            <span>目标读者</span>
            <strong>
                ${escapeHtml(plan.audience || "开发者")}
            </strong>
        </div>

        <div>
            <span>文章类型</span>
            <strong>
                ${escapeHtml(
                    blogKindLabels[plan.blog_kind] || "原理讲解",
                )}
            </strong>
        </div>

        <div>
            <span>写作风格</span>
            <strong>
                ${escapeHtml(plan.tone || "严谨易懂")}
            </strong>
        </div>
    `;

    tasks.forEach((task, index) => {
        const item = document.createElement("article");

        item.className = "plan-item";
        item.dataset.taskId = String(task.id);

        const tags = [];

        if (task.requires_research) {
            tags.push("资料检索");
        }

        if (task.requires_citations) {
            tags.push("引用来源");
        }

        if (task.requires_code) {
            tags.push("代码示例");
        }

        item.innerHTML = `
            <div class="plan-number">
                ${String(index + 1).padStart(2, "0")}
            </div>

            <div class="plan-content">
                <div class="plan-title-row">
                    <h3>
                        ${escapeHtml(task.title)}
                    </h3>

                    <span class="plan-status">
                        等待撰写
                    </span>
                </div>

                <p>
                    ${escapeHtml(task.goal)}
                </p>

                <div class="plan-tags">
                    <span>
                        ${escapeHtml(task.target_words)}
                        字
                    </span>

                    ${tags.map(
                        tag => `
                            <span>
                                ${escapeHtml(tag)}
                            </span>
                        `,
                    ).join("")}
                </div>

                <div class="task-progress">
                    <span></span>
                </div>
            </div>
        `;

        planList.appendChild(item);
    });
}


function completeSection(event) {
    const taskElement = document.querySelector(
        `[data-task-id="${event.task_id}"]`,
    );

    if (taskElement) {
        taskElement.classList.add("completed");

        const status = taskElement.querySelector(
            ".plan-status",
        );

        status.textContent = "已完成";

        const progressBar = taskElement.querySelector(
            ".task-progress span",
        );

        progressBar.style.width = "100%";
    }

    completedTasks = event.completed || (
        completedTasks + 1
    );

    totalTasks = event.total || totalTasks;

    const workerProgress = totalTasks
        ? 35 + (
            completedTasks / totalTasks
        ) * 40
        : 35;

    updateProgress(workerProgress);

    const workerStage = getStageElement("workers");

    if (workerStage) {
        const detail = workerStage.querySelector(
            ".timeline-detail",
        );

        detail.textContent =
            `已完成 ${completedTasks} / ${totalTasks} 个章节。`;
    }
}


function displayFinalResult(event) {
    finalMarkdown = event.markdown || "";

    markdownOutput.textContent = finalMarkdown;

    const rendered = marked.parse(finalMarkdown);

    articlePreview.innerHTML = DOMPurify.sanitize(
        rendered,
    );

    copyButton.disabled = false;

    downloadButton.href = event.download_url;
    downloadButton.classList.remove("disabled");
    downloadButton.setAttribute(
        "aria-disabled",
        "false",
    );

    runBadge.textContent = "已完成";
    runBadge.className = "run-badge completed";

    updateProgress(100);

    document
        .getElementById("resultCard")
        .scrollIntoView({
            behavior: "smooth",
            block: "start",
        });
}


function displayError(message) {
    runBadge.textContent = "生成失败";
    runBadge.className = "run-badge failed";

    const errorItem = document.createElement("div");

    errorItem.className =
        "timeline-item failed";

    errorItem.innerHTML = `
        <div class="timeline-marker">
            <span></span>
        </div>

        <div class="timeline-content">
            <div class="timeline-title">
                文章生成失败
            </div>

            <div class="timeline-detail">
                ${escapeHtml(message)}
            </div>
        </div>
    `;

    timeline.appendChild(errorItem);

    showToast("文章生成失败");
}


function handleEvent(event) {
    switch (event.type) {
        case "run_started":
            runBadge.textContent = "正在生成";
            break;

        case "stage":
            updateStage(event);
            break;

        case "substage":
            addSubstage(event);
            break;

        case "routing":
            showRouting(event);
            break;

        case "research_complete":
            showResearch(event);
            break;

        case "plan":
            showPlan(event.plan || {});
            break;

        case "section_complete":
            completeSection(event);
            break;

        case "images_planned":
            showToast(
                `已规划 ${event.count} 张配图`,
            );
            break;

        case "final":
            displayFinalResult(event);
            break;

        case "error":
            displayError(event.message);
            break;

        case "done":
            setRunningState(false);
            break;
    }
}


function parseSSEChunk(buffer) {
    const events = buffer.split("\n\n");
    const remaining = events.pop() || "";

    events.forEach(block => {
        const dataLines = block
            .split("\n")
            .filter(line => line.startsWith("data:"))
            .map(line => line.slice(5).trim());

        if (!dataLines.length) {
            return;
        }

        const rawData = dataLines.join("\n");

        try {
            const event = JSON.parse(rawData);
            handleEvent(event);
        } catch (error) {
            console.error(
                "无法解析进度事件：",
                rawData,
                error,
            );
        }
    });

    return remaining;
}


async function executeAgent(topic) {
    abortController = new AbortController();

    const response = await fetch("/api/run", {
        method: "POST",

        headers: {
            "Content-Type": "application/json",
        },

        body: JSON.stringify({
            topic,
        }),

        signal: abortController.signal,
    });

    if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const message = typeof payload.detail === "string"
            ? payload.detail
            : response.status === 422
                ? "请输入 2～1000 字的博客主题。"
                : "无法开始写作，请稍后重试。";

        throw new Error(message);
    }

    if (!response.body) {
        throw new Error(
            "当前浏览器不支持流式输出，请使用新版浏览器。",
        );
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    let buffer = "";

    while (true) {
        const {
            done,
            value,
        } = await reader.read();

        if (done) {
            break;
        }

        buffer += decoder.decode(
            value,
            {
                stream: true,
            },
        );

        buffer = parseSSEChunk(buffer);
    }

    buffer += decoder.decode();

    if (buffer.trim()) {
        parseSSEChunk(`${buffer}\n\n`);
    }
}


runForm.addEventListener(
    "submit",
    async event => {
        event.preventDefault();

        const topic = topicInput.value.trim();

        if (topic.length < 2) {
            showToast(
                "请输入至少 2 个字的博客主题。",
            );
            return;
        }

        resetInterface();
        setRunningState(true);

        activityEmpty.hidden = true;
        planEmpty.hidden = false;

        try {
            await executeAgent(topic);
        } catch (error) {
            if (error.name === "AbortError") {
                runBadge.textContent = "已停止";
                runBadge.className =
                    "run-badge stopped";

                showToast(
                    "已停止生成。",
                );
            } else {
                displayError(
                    error.message
                    || "生成时发生错误，请稍后重试。",
                );
            }
        } finally {
            setRunningState(false);
            abortController = null;
        }
    },
);


cancelButton.addEventListener(
    "click",
    () => {
        if (abortController) {
            abortController.abort();
        }
    },
);


newRunButton.addEventListener(
    "click",
    () => {
        if (abortController) {
            abortController.abort();
        }

        resetInterface();

        topicInput.disabled = false;
        topicInput.value = "";
        topicInput.focus();
    },
);


document
    .querySelectorAll("[data-topic]")
    .forEach(button => {
        button.addEventListener(
            "click",
            () => {
                topicInput.value =
                    button.dataset.topic || "";

                topicInput.focus();
            },
        );
    });


previewTab.addEventListener(
    "click",
    () => {
        previewTab.classList.add("active");
        markdownTab.classList.remove("active");

        articlePreview.hidden = false;
        markdownOutput.hidden = true;
    },
);


markdownTab.addEventListener(
    "click",
    () => {
        markdownTab.classList.add("active");
        previewTab.classList.remove("active");

        markdownOutput.hidden = false;
        articlePreview.hidden = true;
    },
);


copyButton.addEventListener(
    "click",
    async () => {
        if (!finalMarkdown) {
            return;
        }

        await navigator.clipboard.writeText(
            finalMarkdown,
        );

        showToast(
            "已复制 Markdown。",
        );
    },
);


downloadButton.addEventListener(
    "click",
    event => {
        if (
            downloadButton.classList.contains(
                "disabled",
            )
        ) {
            event.preventDefault();
        }
    },
);


async function checkHealth() {
    try {
        const response = await fetch(
            "/api/health",
        );

        if (!response.ok) {
            throw new Error("服务暂不可用");
        }

        healthDot.classList.add("online");
        healthText.textContent = "服务已连接";
    } catch {
        healthDot.classList.remove("online");
        healthText.textContent = "服务未连接";
    }
}


resetInterface();
checkHealth();