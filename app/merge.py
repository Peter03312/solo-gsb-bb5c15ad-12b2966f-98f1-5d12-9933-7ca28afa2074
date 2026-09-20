"""按 cue id 执行三方合并的纯逻辑层。

合并规则（相对 ``base`` 共同祖先）：

* 只有一侧修改了某字段 → 采纳该侧的值；
* 两侧都未修改 → 保留 base 的值；
* 两侧都改成了相同的值 → 采纳该值；
* 两侧改成了不同的值 → 记录字段级冲突；
* 一侧删除、另一侧修改 → 记录 cue 级冲突（delete_modify）；
* 两侧都删除 → 干净删除；
* 两侧各自新增同一 id：取值一致则采纳，不一致记录 add_add 冲突。

仅当不存在任何冲突时才检查时间轴：合并结果按开始时间排序后，
若存在区间重叠（区间相交且重叠时长大于 0，仅首尾相接不算重叠），
则报告所有重叠的 cue id 及重叠区间，不输出合并版本。
"""

from __future__ import annotations

from typing import Optional

from .models import (
    MERGE_FIELDS,
    Cue,
    CueConflict,
    FieldConflict,
    Overlap,
)


def _index(cues: list[Cue]) -> dict[str, Cue]:
    return {cue.id: cue for cue in cues}


def _changed(field: str, base: Optional[Cue], side: Optional[Cue]) -> bool:
    """相对于 base，side 是否修改了 field。

    对于缺失的可选值（speaker）统一归一化为 None，
    使 None 与“未提供”等价。
    """
    base_val = getattr(base, field, None)
    side_val = getattr(side, field, None)
    return side_val != base_val


def three_way_merge(
    base: list[Cue], current: list[Cue], incoming: list[Cue]
) -> tuple[list[Cue], list[CueConflict], list[FieldConflict]]:
    """返回 (合并后未排序的 cues, cue 级冲突, 字段级冲突)。"""
    base_map, cur_map, inc_map = _index(base), _index(current), _index(incoming)

    # 稳定的 id 顺序：先 base，再 current 新增，再 incoming 新增
    ordered_ids: list[str] = []
    for cue in base:
        ordered_ids.append(cue.id)
    for cue in current:
        if cue.id not in base_map and cue.id not in ordered_ids:
            ordered_ids.append(cue.id)
    for cue in incoming:
        if cue.id not in base_map and cue.id not in cur_map and cue.id not in ordered_ids:
            ordered_ids.append(cue.id)

    merged: list[Cue] = []
    cue_conflicts: list[CueConflict] = []
    field_conflicts: list[FieldConflict] = []

    for cue_id in ordered_ids:
        b = base_map.get(cue_id)
        c = cur_map.get(cue_id)
        i = inc_map.get(cue_id)

        if b is not None:
            c_deleted = c is None
            i_deleted = i is None

            if c_deleted and i_deleted:
                # 两侧都删除：干净删除
                continue
            if c_deleted or i_deleted:
                survivor = i if c_deleted else c
                changed_fields = [f for f in MERGE_FIELDS if _changed(f, b, survivor)]
                if not changed_fields:
                    # 一侧删除、另一侧未改：删除方的变化生效
                    continue
                # 一侧删除、另一侧修改 → 必须报告冲突
                cue_conflicts.append(
                    CueConflict(
                        cue_id=cue_id,
                        type="delete_modify",
                        side="current" if c_deleted else "incoming",
                        fields=changed_fields,
                        current=c,
                        incoming=i,
                    )
                )
                continue

            # 两侧都存活：逐字段合并
            values: dict[str, object] = {"id": cue_id}
            cue_has_conflict = False
            for field in MERGE_FIELDS:
                base_val = getattr(b, field)
                cur_val = getattr(c, field)
                inc_val = getattr(i, field)
                cur_changed = cur_val != base_val
                inc_changed = inc_val != base_val

                if not cur_changed and not inc_changed:
                    values[field] = base_val
                elif cur_changed and not inc_changed:
                    values[field] = cur_val
                elif inc_changed and not cur_changed:
                    values[field] = inc_val
                elif cur_val == inc_val:
                    values[field] = cur_val  # 两侧改成同一个值
                else:
                    field_conflicts.append(
                        FieldConflict(
                            cue_id=cue_id,
                            field=field,
                            base=base_val,
                            current=cur_val,
                            incoming=inc_val,
                        )
                    )
                    cue_has_conflict = True

            if not cue_has_conflict:
                try:
                    merged.append(Cue(**values))
                except ValueError:
                    # 字段本身均可合并，但合并后 end <= start（两侧分别改了两端）
                    cue_conflicts.append(
                        CueConflict(
                            cue_id=cue_id,
                            type="invalid_interval",
                            side=None,
                            fields=["start", "end"],
                            current=c,
                            incoming=i,
                        )
                    )
            # 有字段冲突的 cue 不进入 merged
        else:
            # base 中不存在 → 新增
            if c is not None and i is not None:
                differing = [f for f in MERGE_FIELDS if getattr(c, f) != getattr(i, f)]
                if differing:
                    cue_conflicts.append(
                        CueConflict(
                            cue_id=cue_id,
                            type="add_add",
                            fields=differing,
                            current=c,
                            incoming=i,
                        )
                    )
                    continue
                merged.append(c.model_copy(deep=True))
            else:
                # 仅一侧新增
                added = c if c is not None else i
                merged.append(added.model_copy(deep=True))

    return merged, cue_conflicts, field_conflicts


def find_overlaps(cues: list[Cue]) -> list[Overlap]:
    """在按开始时间排序的 cue 列表上查找所有区间重叠对。

    仅首尾相接（上一条 end == 下一条 start）不算重叠。
    """
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end, cue.id))
    overlaps: list[Overlap] = []
    for idx, cue in enumerate(ordered):
        for other in ordered[idx + 1 :]:
            if other.start >= cue.end:
                break  # 后续 cue 起点更晚，不可能再与 cue 重叠
            overlap_end = min(cue.end, other.end)
            overlap_start = max(cue.start, other.start)
            overlap_len = overlap_end - overlap_start
            if overlap_len > 0:
                overlaps.append(
                    Overlap(
                        cue_ids=[cue.id, other.id],
                        start=overlap_start,
                        end=overlap_end,
                        overlap=overlap_len,
                    )
                )
    overlaps.sort(key=lambda item: (item.start, item.end, *item.cue_ids))
    return overlaps
