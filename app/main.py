"""FastAPI 应用：字幕时间轴三方合并服务。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .merge import (
    apply_decisions,
    find_overlaps,
    three_way_merge,
    validate_decisions,
)
from .models import (
    HealthResponse,
    MergeConflictResponse,
    MergeOverlapResponse,
    MergeRequest,
    MergeSuccessResponse,
    ResolveInvalidTimelineResponse,
    ResolveRequest,
    ResolveSuccessResponse,
)

app = FastAPI(
    title="字幕时间轴三方合并 API",
    description=(
        "POST /merge 接收 base / current / incoming 三版 cue 列表，"
        "按 cue id 进行三方合并、字段级冲突检测与时间轴重叠检测；"
        "POST /resolve 在同样的三方合并之上接收人工决策解决全部冲突，"
        "再统一校验解析后的时间轴。"
    ),
    version=__version__,
)


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    """服务根路径，指向交互式文档。"""
    return {"service": "subtitle-merge", "version": __version__, "docs": "/docs"}


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """健康检查。"""
    return HealthResponse(status="ok")


@app.post(
    "/merge",
    tags=["merge"],
    response_model=MergeSuccessResponse,
    responses={
        409: {
            "model": MergeConflictResponse | MergeOverlapResponse,
            "description": (
                "合并冲突（字段级冲突、删除/修改冲突等），或无冲突但时间轴重叠。"
                "两种情况下均不输出合并版本。"
            ),
        }
    },
)
def merge(request: MergeRequest):
    """三方合并字幕 cue 列表。"""
    merged, cue_conflicts, field_conflicts = three_way_merge(
        request.base, request.current, request.incoming
    )

    if cue_conflicts or field_conflicts:
        content = MergeConflictResponse(
            cue_conflicts=cue_conflicts,
            field_conflicts=field_conflicts,
        ).model_dump()
        return JSONResponse(status_code=409, content=content)

    overlaps = find_overlaps(merged)
    if overlaps:
        content = MergeOverlapResponse(overlaps=overlaps).model_dump()
        return JSONResponse(status_code=409, content=content)

    merged.sort(key=lambda cue: (cue.start, cue.end, cue.id))
    return MergeSuccessResponse(cues=merged, count=len(merged))


@app.post(
    "/resolve",
    tags=["merge"],
    response_model=ResolveSuccessResponse,
    responses={
        409: {
            "model": ResolveInvalidTimelineResponse,
            "description": (
                "全部决策应用后统一校验失败：形成非法区间（end <= start）"
                "或时间轴重叠。不输出任何部分结果。"
            ),
        },
        422: {
            "description": (
                "决策非法：重复、指向不存在的冲突（unknown）、"
                "决策类型与冲突类型不符（type_mismatch）或遗漏冲突（missing）。"
            ),
        },
    },
)
def resolve(request: ResolveRequest):
    """携带人工决策完成三方合并。

    复用与 /merge 相同的三方合并识别全部冲突；field_choices 按
    ``cue_id + field`` 解决字段级冲突，cue_choices 按 ``cue_id`` 解决
    cue 级冲突（current / incoming / delete）。决策通过校验后才统一应用，
    随后对完整时间轴做区间合法性与重叠校验。
    """
    _, cue_conflicts, field_conflicts = three_way_merge(
        request.base, request.current, request.incoming
    )

    errors, field_decisions, cue_decisions = validate_decisions(
        cue_conflicts,
        field_conflicts,
        request.field_choices,
        request.cue_choices,
    )
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    err.model_dump(exclude_none=True) for err in errors
                ]
            },
        )

    resolved, invalid_interval_ids = apply_decisions(
        request.base,
        request.current,
        request.incoming,
        cue_conflicts,
        field_conflicts,
        field_decisions,
        cue_decisions,
    )

    # 应用全部决策后统一校验：任何非法区间或重叠都不输出部分结果
    valid = [cue for cue in resolved if cue.id not in set(invalid_interval_ids)]
    overlaps = find_overlaps(valid)
    if invalid_interval_ids or overlaps:
        content = ResolveInvalidTimelineResponse(
            invalid_intervals=invalid_interval_ids,
            overlaps=overlaps,
        ).model_dump()
        return JSONResponse(status_code=409, content=content)

    resolved.sort(key=lambda cue: (cue.start, cue.end, cue.id))
    return ResolveSuccessResponse(cues=resolved, count=len(resolved))
