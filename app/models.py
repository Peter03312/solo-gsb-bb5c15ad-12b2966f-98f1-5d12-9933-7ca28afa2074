"""请求与响应的 Pydantic 模型。

时间单位统一为秒（支持小数）。一条 cue 表示一个字幕区间：
``start`` 为区间起点，``end`` 为区间终点（必须大于 ``start``），
``text`` 为字幕文本，``speaker`` 为可选的说话人。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

# 参与三方合并的字段（id 是合并键，不在此列）
MERGE_FIELDS: tuple[str, ...] = ("start", "end", "text", "speaker")


class Cue(BaseModel):
    """单条字幕。"""

    id: str = Field(..., min_length=1, description="字幕唯一标识，三方合并以此为准")
    start: float = Field(..., ge=0, description="开始时间（秒）")
    end: float = Field(..., ge=0, description="结束时间（秒），必须大于 start")
    text: str = Field(..., description="字幕文本")
    speaker: Optional[str] = Field(None, description="说话人（可选）")

    @model_validator(mode="after")
    def _check_interval(self) -> "Cue":
        if self.end <= self.start:
            raise ValueError("end 必须大于 start")
        return self


class MergeRequest(BaseModel):
    """POST /merge 的请求体：base / current / incoming 三版 cue 列表。"""

    base: list[Cue] = Field(default_factory=list, description="共同祖先版本")
    current: list[Cue] = Field(default_factory=list, description="当前方（我方）修订")
    incoming: list[Cue] = Field(default_factory=list, description="外部方修订")

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "MergeRequest":
        for name in ("base", "current", "incoming"):
            ids = [cue.id for cue in getattr(self, name)]
            if len(ids) != len(set(ids)):
                duplicated = sorted({cue_id for cue_id in ids if ids.count(cue_id) > 1})
                raise ValueError(f"{name} 中存在重复的 cue id: {', '.join(duplicated)}")
        return self


class CueConflict(BaseModel):
    """单条 cue 级别的冲突说明。"""

    cue_id: str
    type: Literal["delete_modify", "add_add", "invalid_interval"] = Field(
        ...,
        description=(
            "delete_modify：一侧删除另一侧修改；"
            "add_add：两侧各自新增同一 id；"
            "invalid_interval：字段可合并但合并结果不是合法区间（end <= start）"
        ),
    )
    side: Optional[Literal["current", "incoming"]] = Field(
        None, description="delete_modify 时发生删除的一侧"
    )
    fields: list[str] = Field(
        default_factory=list,
        description="add_add 时两侧取值不一致的字段",
    )
    current: Optional[Cue] = Field(None, description="current 侧该 cue 的内容（若存在）")
    incoming: Optional[Cue] = Field(None, description="incoming 侧该 cue 的内容（若存在）")


class FieldConflict(BaseModel):
    """同一 cue 存活状态下，两侧对同一字段给出不同新值的字段级冲突。"""

    cue_id: str
    field: Literal["start", "end", "text", "speaker"]
    base: Optional[object] = Field(None, description="共同祖先中的原值")
    current: Optional[object] = Field(None, description="current 侧的新值")
    incoming: Optional[object] = Field(None, description="incoming 侧的新值")


class Overlap(BaseModel):
    """一对重叠 cue 及其重叠区间。"""

    cue_ids: list[str] = Field(..., min_length=2, max_length=2)
    start: float = Field(..., description="重叠区间起点（秒）")
    end: float = Field(..., description="重叠区间终点（秒）")
    overlap: float = Field(..., description="重叠时长（秒）")


class MergeConflictResponse(BaseModel):
    """存在合并冲突时的 409 响应。"""

    status: Literal["conflict"] = "conflict"
    cue_conflicts: list[CueConflict] = Field(default_factory=list)
    field_conflicts: list[FieldConflict] = Field(default_factory=list)


class MergeOverlapResponse(BaseModel):
    """无合并冲突、但合并结果时间轴存在重叠时的 409 响应（不输出合并版本）。"""

    status: Literal["overlap"] = "overlap"
    overlaps: list[Overlap]


class MergeSuccessResponse(BaseModel):
    """合并成功：结果已按开始时间排序。"""

    status: Literal["merged"] = "merged"
    cues: list[Cue]
    count: int


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: Literal["ok"] = "ok"
