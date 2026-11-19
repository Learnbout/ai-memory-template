"""Pydantic 模型：与 CONTRACT.md §5 严格一致（字段名、枚举值、JSON 结构）。

枚举取值（§5）：
- ToolCategory = 检索类 | 读写类 | 元数据
- RunType      = MCP 调用 | Agent 检索
- RunStatus    = 成功 | 失败 | 运行中 | 警告
- SearchMode   = keyword | semantic | hybrid
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 5.1 Vault 状态
# --------------------------------------------------------------------------- #
class VaultStatus(BaseModel):
    mounted: bool
    noteCount: int
    path: str
    label: str
    error: Optional[str] = None  # §9.10：路径不可读时给原因，不抛 500
    # T2（2026-10-19）数据源开关：不声明这几个字段，接口层面「就是没返回」
    # —— 本项目 Pydantic 静默丢字段已复发 3 次，这里是第 4 次预防。
    source: str = "md"  # md | db
    sourceLabel: str = "Markdown 文件"
    dbBuiltAt: Optional[str] = None
    indexMs: Optional[float] = None


# --------------------------------------------------------------------------- #
# 9.2 / 9.3 笔记（覆盖 §5.2 / §5.3）
# --------------------------------------------------------------------------- #
class LinkRef(BaseModel):
    id: str
    title: str
    relPath: str = ""


class HitRecord(BaseModel):
    query: str
    score: float
    at: str


class NoteMetadata(BaseModel):
    created: str
    updated: str
    wordCount: int
    sizeKB: float
    folder: str
    type: str
    accessCount: int = 0
    verified: bool = False
    version: int = 0
    source: str = ""
    outCount: int
    inCount: int


class TaskStats(BaseModel):
    done: int
    open: int


class NoteSummary(BaseModel):
    id: str
    title: str
    relPath: str
    folder: str
    type: str
    typeLabel: str
    tags: List[str]
    # 抽屉分层 hot / warm / cold；未分层为空串（看板「热度分层」卡点击后按它筛列表）
    tier: str = ""
    # 阶段 6（2026-10-23）：重要性等级 S/A/B/C；旧库无列为空串（前端隐藏徽章）。
    # **必须在这里声明**，否则 response_model 会把它静默砍掉 —— 本项目此坑已踩 5 次。
    grade: str = ""
    excerpt: str
    updatedAt: str
    updatedLabel: str
    # 添加时间（frontmatter created；缺失时回落 updated）。**必须在这里声明**，
    # 否则 response_model 会把它静默砍掉 —— 这个坑本项目已经踩过 4 次。
    createdAt: str = ""
    wordCount: int
    sizeKB: float
    degree: int


class NoteDetail(NoteSummary):
    content: str
    readingOnly: bool
    frontmatter: Dict[str, str]
    outlinks: List[LinkRef]
    backlinks: List[LinkRef]
    dangling: List[str]
    metadata: NoteMetadata
    taskStats: TaskStats
    hits: List[HitRecord]


# --------------------------------------------------------------------------- #
# 9.4 facets
# --------------------------------------------------------------------------- #
class FacetType(BaseModel):
    key: str
    label: str
    count: int


class FacetTag(BaseModel):
    tag: str
    count: int


class FacetFolder(BaseModel):
    folder: str
    count: int


class FacetTotals(BaseModel):
    notes: int
    links: int
    resolvedLinks: int
    dangling: int
    orphans: int


class FacetsData(BaseModel):
    types: List[FacetType]
    tags: List[FacetTag]
    folders: List[FacetFolder]
    totals: FacetTotals


# --------------------------------------------------------------------------- #
# 9.5 graph
# --------------------------------------------------------------------------- #
class GraphNode(BaseModel):
    id: str
    title: str
    relPath: str
    folder: str
    type: str
    degree: int
    inDegree: int
    outDegree: int


class GraphLink(BaseModel):
    source: str
    target: str


class GraphStats(BaseModel):
    nodes: int
    links: int
    # 2026-10-20 新增。**必须在这里显式声明**：本文件是 FastAPI 的 response_model，
    # Pydantic 默认丢弃未声明的字段 —— 只在 vault.graph() 里加键的话，
    # 接口层会静默把它抹掉（实测：返回体里就是没有，日志里一个警告都没有）。
    # 给默认值是为了兼容「后端先更新、前端后更新」的过渡期。
    uniqueLinks: Optional[int] = None
    dangling: int
    danglingTitles: Optional[int] = None
    orphans: int
    maxDegree: int


class GraphData(BaseModel):
    nodes: List[GraphNode]
    links: List[GraphLink]
    folders: List[FacetFolder]
    stats: GraphStats


class ReindexResult(BaseModel):
    ok: bool
    notes: int
    links: int
    ms: int


# --------------------------------------------------------------------------- #
# 5.4 工具定义
# --------------------------------------------------------------------------- #
class ToolParam(BaseModel):
    name: str
    type: str
    required: bool = False
    default: Optional[Any] = None
    enum: Optional[List[str]] = None
    desc: str = ""


class ToolDef(BaseModel):
    name: str
    category: str
    risk: str = "normal"
    summary: str
    params: List[ToolParam]
    example: Dict[str, Any]
    # 全量工具目录扩展字段（新增契约，向后兼容：缺省即视为本工作台内置工具）
    source: str = "workspace"
    sourceLabel: str = "本工作台"
    badges: List[str] = Field(default_factory=list)
    invokable: bool = True
    note: Optional[str] = None
    externalServer: bool = False


class ToolSource(BaseModel):
    """工具来源汇总（GET /api/tools/sources）。"""

    key: str
    label: str
    kind: str
    connected: bool
    toolCount: int
    invokableCount: int
    exposedCount: Optional[int] = None
    description: str = ""
    note: Optional[str] = None


# --------------------------------------------------------------------------- #
# 5.5 工具调用
# --------------------------------------------------------------------------- #
class InvokeError(BaseModel):
    code: str
    message: str


class InvokeResult(BaseModel):
    status: str
    elapsedMs: int
    mockId: str
    toolName: str
    result: List[Dict[str, Any]]
    error: Optional[InvokeError] = None
    log: List[str]


# --------------------------------------------------------------------------- #
# 5.6 运行记录
# --------------------------------------------------------------------------- #
class RunRecord(BaseModel):
    id: str
    at: str
    dateLabel: str
    timeLabel: str
    type: str
    target: str
    status: str
    elapsedMs: int
    stepLabel: str
    caller: str = "手动"  # 调用方："Agent" | "手动"（§5.8 派生用，缺省 "手动"）
    detail: Dict[str, Any] = Field(default_factory=dict)


class RunPage(BaseModel):
    total: int
    page: int
    pageSize: int
    items: List[RunRecord]


class OkResponse(BaseModel):
    ok: bool


# --------------------------------------------------------------------------- #
# 5.7 Agent Search
# --------------------------------------------------------------------------- #
class AgentSearchRequest(BaseModel):
    query: str = ""
    mode: Optional[str] = None  # 为空时取设置 search.mode（§9.9）
    topK: Optional[int] = None  # 为空时取设置 search.topK（§9.9）
    scope: str = "全部笔记"


class AgentStep(BaseModel):
    index: int
    title: str
    detail: str
    status: str


class Evidence(BaseModel):
    rank: int
    id: str
    title: str
    score: float
    relPath: str
    quote: str


class AgentAnswer(BaseModel):
    text: str
    citations: List[int]
    confidence: str


class AgentStats(BaseModel):
    elapsedMs: int
    toolCalls: int
    toolNames: List[str]
    citedNotes: int


class TraceItem(BaseModel):
    t: str
    step: str
    note: str


class AgentRun(BaseModel):
    id: str
    query: str
    status: str
    steps: List[AgentStep]
    narration: str
    evidence: List[Evidence]
    answer: AgentAnswer
    stats: AgentStats
    trace: List[TraceItem]


# --------------------------------------------------------------------------- #
# 5.8 Dashboard（2026-10-14 改版：对齐第二版设计稿 new-dashboard-ref.png）
# --------------------------------------------------------------------------- #
class DashUser(BaseModel):
    name: str
    role: str
    initial: str


class DashAction(BaseModel):
    label: str
    to: str
    variant: str


class DashHero(BaseModel):
    greeting: str
    description: str
    actions: List[DashAction]


class DashStatItem(BaseModel):
    key: str
    icon: str
    value: str
    label: str
    delta: str
    deltaDir: str
    sample: bool
    # 迷你折线图（sparkline）：近 N 天日度序列，索引末位 = 今天
    trend: List[int] = []
    trendLabel: str = ""


class DashRecentNote(BaseModel):
    id: str
    title: str
    relPath: str
    timeLabel: str
    tag: str


class DashTodoItem(BaseModel):
    # 来源笔记 id / 路径：前端点待办可跳转到对应笔记（§17 新增）
    id: str = ""
    relPath: str = ""
    title: str
    source: str
    timeLabel: str
    done: bool


class DashTodos(BaseModel):
    totalLabel: str
    items: List[DashTodoItem]


class DashDistItem(BaseModel):
    """横向条形卡里的一行。"""

    key: str
    label: str
    count: int
    # 占全库比例（0..1）；条形的相对长度由前端用 count/max(count) 现算
    share: float = 0.0


class DashDist(BaseModel):
    """横向条形卡：热度分层（tier） / 记忆类型（type）。"""

    title: str
    note: str
    total: int
    items: List[DashDistItem]


class DashProject(BaseModel):
    # 点项目名跳笔记要这两个：只给 name 就点不动（Pydantic 会把未声明的键直接丢掉，
    # 所以 2026-10-20 给 dashboard 加字段时必须同步在这里声明，否则前端拿到的永远是 null）
    id: str = ""
    relPath: str = ""
    name: str
    status: str
    statusKind: str
    progress: Optional[int] = None  # §9.8：无 checkbox → null


class DashMeta(BaseModel):
    icon: str
    label: str


class DashActionLink(BaseModel):
    label: str
    to: str


class DashSearchSummary(BaseModel):
    badge: str
    query: str
    conclusion: str
    meta: List[DashMeta]
    action: DashActionLink


class DashCallItem(BaseModel):
    time: str
    tool: str
    caller: str
    status: str
    elapsed: str


class DashRecentCalls(BaseModel):
    note: str
    items: List[DashCallItem]


class DashCalendar(BaseModel):
    title: str
    monthLabel: str
    year: int
    month: int
    today: int
    markers: List[int]
    weekdays: List[str]


class DashGraphStats(BaseModel):
    nodes: int
    links: int
    dangling: int
    orphans: int


class DashGraph(BaseModel):
    title: str
    note: str
    stats: DashGraphStats
    nodes: List[GraphNode]
    links: List[GraphLink]


class DashHotTag(BaseModel):
    tag: str
    # 不带井号的原标签值：点标签跳 /memory-explorer?tag=<key> 用它（笔记 tags 无井号）
    key: str = ""
    count: int


class DashHotTags(BaseModel):
    title: str
    items: List[DashHotTag]


class DashMcpConnection(BaseModel):
    key: str = ""
    name: str
    connected: bool
    badge: str
    # 已连接时服务端 tools/list 枚举出的工具数（前端不显示，留给排查用）
    toolCount: int = 0


class DashMcpConnections(BaseModel):
    title: str
    items: List[DashMcpConnection]
    note: Optional[str] = None


class DashboardData(BaseModel):
    user: DashUser
    hero: DashHero
    stats: List[DashStatItem]
    recentNotes: List[DashRecentNote]
    todos: DashTodos
    projects: List[DashProject]
    # 全库「近 30 天有更新」的项目数（卡头「最近 3 / 共 N 个活跃」）。缺省 0 = 老客户端兜底
    projectsTotal: int = 0
    searchSummary: DashSearchSummary
    recentCalls: DashRecentCalls
    calendar: DashCalendar
    graph: DashGraph
    tierDist: DashDist
    typeDist: DashDist
    hotTags: DashHotTags
    mcpConnections: DashMcpConnections


# --------------------------------------------------------------------------- #
# 5.9 设置
# --------------------------------------------------------------------------- #
class SettingsItem(BaseModel):
    key: str
    label: str
    type: str
    value: Any
    options: Optional[List[str]] = None
    hint: Optional[str] = None
    # slider 专用（Pydantic 未声明字段会被静默丢弃 —— 本项目同型坑第 3 次，别删）
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None


class SettingsSection(BaseModel):
    key: str
    title: str
    items: List[SettingsItem]


class SettingsData(BaseModel):
    sections: List[SettingsSection]


# --------------------------------------------------------------------------- #
# 9.11 活动日历（Dashboard 日期面板真数据化）
# --------------------------------------------------------------------------- #
class CalendarDayCell(BaseModel):
    """月视图里的一天：新增文件数 + 仅更新数，level 按新增数分档。"""

    date: str  # YYYY-MM-DD
    day: int  # 1..31
    added: int  # 当天新增的文件数（frontmatter created 落该日）
    updated: int  # 当天「仅被更新」的文件数（updated 落该日，且不是当天新增）
    level: int  # 0..4（按当月新增峰值等分；0 = 当天无新增）


class CalendarMonthData(BaseModel):
    year: int
    month: int
    monthLabel: str  # "2026年10月"
    today: int  # 不在本月时为 0
    weekdays: List[str]
    days: List[CalendarDayCell]
    maxAdded: int  # 当月单日新增峰值（level 的分档基准）
    totalAdded: int  # 当月新增文件总数
    totalUpdated: int  # 当月「仅更新」文件总数
    addedDays: int  # 当月有新增的天数


class CalendarDayFile(BaseModel):
    """下钻列表里的一行文件（新增 or 仅更新）。"""

    id: str
    title: str
    relPath: str
    folder: str
    type: str
    typeLabel: str
    tags: List[str]
    excerpt: str
    event: str  # "created"（当天新增）| "updated"（当天仅更新）
    timeLabel: str  # event 对应的 HH:MM
    # timeLabel 的来源："created" | "updated" | "updated-fallback"
    # （updated-fallback = created 只写到日、没有钟点，时刻回落到该文件 updated）
    # ⚠ 这里必须显式声明：Pydantic 响应模型会**静默丢弃**未声明字段，
    #   漏掉的后果是前端拿不到 timeSource、虚线下划线的「推断」标记永远不亮。
    timeSource: str
    createdAt: str
    updatedAt: str
    wordCount: int
    sizeKB: float
    degree: int


class CalendarDayTypeCount(BaseModel):
    key: str
    label: str
    count: int


class CalendarDayTagCount(BaseModel):
    tag: str
    count: int


class CalendarDayData(BaseModel):
    date: str
    label: str  # "10月14日 周三"
    isToday: bool
    addedCount: int
    updatedCount: int
    runCount: int  # 当天工具调用次数（仅计数，明细在 /run-history）
    added: List[CalendarDayFile]
    updated: List[CalendarDayFile]
    types: List[CalendarDayTypeCount]  # 新增文件的类型分布
    tags: List[CalendarDayTagCount]  # 新增文件的标签分布
