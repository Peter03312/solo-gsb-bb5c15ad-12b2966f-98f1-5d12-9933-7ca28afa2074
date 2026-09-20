"""FastAPI 应用：字幕时间轴三方合并服务。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .merge import find_overlaps, three_way_merge
from .models import (
    HealthResponse,
    MergeConflictResponse,
    MergeOverlapResponse,
    MergeRequest,
    MergeSuccessResponse,
)

app = FastAPI(
    title="字幕时间轴三方合并 API",
    description=(
        "POST /merge 接收 base / current / incoming 三版 cue 列表，"
        "按 cue id 进行三方合并、字段级冲突检测与时间轴重叠检测。"
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
