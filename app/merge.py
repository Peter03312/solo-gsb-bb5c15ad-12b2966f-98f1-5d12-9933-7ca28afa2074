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

from dataclasses import dataclass, field
from typing import Optional

from .models import (
    MERGE_FIELDS,
    Cue,
    CueConflict,
    DecisionError,
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


def _merge_live_cue(
    cue_id: str, b: Cue, c: Cue, i: Cue
) -> tuple[Optional[Cue], list[FieldConflict], Optional[CueConflict]]:
    """合并两侧都存活的同一 cue（相对 base 逐字段合并）。

    返回 (合并后 cue 或 None, 字段级冲突, cue 级冲突)：
    存在字段级冲突或合并后区间非法时 cue 为 None，
    且两者至多其一非 None。
    """
    values: dict[str, object] = {"id": cue_id}
    field_conflicts: list[FieldConflict] = []
    for fld in MERGE_FIELDS:
        base_val = getattr(b, fld)
        cur_val = getattr(c, fld)
        inc_val = getattr(i, fld)
        cur_changed = cur_val != base_val
        inc_changed = inc_val != base_val

        if not cur_changed and not inc_changed:
            values[fld] = base_val
        elif cur_changed and not inc_changed:
            values[fld] = cur_val
        elif inc_changed and not cur_changed:
            values[fld] = inc_val
        elif cur_val == inc_val:
            values[fld] = cur_val  # 两侧改成同一个值
        else:
            field_conflicts.append(
                FieldConflict(
                    cue_id=cue_id,
                    field=fld,
                    base=base_val,
                    current=cur_val,
                    incoming=inc_val,
                )
            )

    if field_conflicts:
        return None, field_conflicts, None

    try:
        return Cue(**values), [], None
    except ValueError:
        # 字段本身均可合并，但合并后 end <= start（两侧分别改了两端）
        return None, [], CueConflict(
            cue_id=cue_id,
            type="invalid_interval",
            side=None,
            fields=["start", "end"],
            current=c,
            incoming=i,
        )


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
            cue, cue_field_conflicts, cue_cue_conflict = _merge_live_cue(
                cue_id, b, c, i
            )
            if cue is not None:
                merged.append(cue)
            else:
                field_conflicts.extend(cue_field_conflicts)
                if cue_cue_conflict is not None:
                    cue_conflicts.append(cue_cue_conflict)
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


@dataclass
class Resolution:
    """resolve_three_way 的结果。

    存在决策错误时只填充 errors；否则填充 resolved/invalid_intervals/overlaps。
    """

    errors: list[DecisionError] = field(default_factory=list)
    resolved: list[Cue] = field(default_factory=list)
    invalid_intervals: list[str] = field(default_factory=list)
    overlaps: list[Overlap] = field(default_factory=list)


def resolve_three_way(
    base: list[Cue],
    current: list[Cue],
    incoming: list[Cue],
    field_choices: list[tuple[str, str, str]],
    cue_choices: list[tuple[str, str]],
) -> Resolution:
    """在三方合并识别出的冲突之上应用人工决策。

    field_choices 为 (cue_id, field, choice)，choice ∈ base/current/incoming；
    cue_choices 为 (cue_id, choice)，choice ∈ current/incoming/delete。

    决策存在重复、未知、类型不匹配或遗漏时，errors 非空且不应用任何决策；
    否则应用全部决策后统一构造与校验 Cue，收集非法区间与时间轴重叠
    （任一存在时由调用方返回 409，不输出部分结果）。
    """
    merged, cue_conflicts, field_conflicts = three_way_merge(
        base, current, incoming
    )

    field_keys = {(fc.cue_id, fc.field) for fc in field_conflicts}
    field_conflict_ids = {fc.cue_id for fc in field_conflicts}
    cue_keys = {cc.cue_id for cc in cue_conflicts}
    cue_conflict_map = {cc.cue_id: cc for cc in cue_conflicts}

    errors: list[DecisionError] = []

    # —— 校验 field_choices ——
    seen_field: set[tuple[str, str]] = set()
    dup_field: set[tuple[str, str]] = set()
    for cue_id, fld, _choice in field_choices:
        key = (cue_id, fld)
        if key in seen_field:
            dup_field.add(key)
        seen_field.add(key)
    for cue_id, fld in sorted(dup_field):
        errors.append(
            DecisionError(
                cue_id=cue_id,
                field=fld,
                type="duplicate",
                message=f"cue {cue_id} 的字段 {fld} 存在多条字段决策",
            )
        )

    for cue_id, fld, _choice in field_choices:
        key = (cue_id, fld)
        if key in dup_field:
            continue
        if cue_id in cue_keys:
            errors.append(
                DecisionError(
                    cue_id=cue_id,
                    field=fld,
                    type="type_mismatch",
                    message=(
                        f"cue {cue_id} 存在 cue 级冲突"
                        f"（{cue_conflict_map[cue_id].type}），须使用 cue_choices 决策"
                    ),
                )
            )
        elif key not in field_keys:
            errors.append(
                DecisionError(
                    cue_id=cue_id,
                    field=fld,
                    type="unknown",
                    message=f"cue {cue_id} 的字段 {fld} 不存在字段级冲突",
                )
            )

    # —— 校验 cue_choices ——
    seen_cue: set[str] = set()
    dup_cue: set[str] = set()
    for cue_id, _choice in cue_choices:
        if cue_id in seen_cue:
            dup_cue.add(cue_id)
        seen_cue.add(cue_id)
    for cue_id in sorted(dup_cue):
        errors.append(
            DecisionError(
                cue_id=cue_id,
                type="duplicate",
                message=f"cue {cue_id} 存在多条 cue 决策",
            )
        )

    for cue_id, _choice in cue_choices:
        if cue_id in dup_cue:
            continue
        if cue_id in field_conflict_ids:
            errors.append(
                DecisionError(
                    cue_id=cue_id,
                    type="type_mismatch",
                    message=f"cue {cue_id} 的冲突为字段级，须使用 field_choices 决策",
                )
            )
        elif cue_id not in cue_keys:
            errors.append(
                DecisionError(
                    cue_id=cue_id,
                    type="unknown",
                    message=f"cue {cue_id} 不存在 cue 级冲突",
                )
            )

    # —— 遗漏的冲突必须给出决策（重复决策已单独报错，不再级联 missing）——
    decided_field = {(cue_id, fld) for cue_id, fld, _choice in field_choices}
    decided_cue = {cue_id for cue_id, _choice in cue_choices}
    for key in sorted(field_keys - decided_field):
        cue_id, fld = key
        errors.append(
            DecisionError(
                cue_id=cue_id,
                field=fld,
                type="missing",
                message=f"缺少 cue {cue_id} 字段 {fld} 的字段决策",
            )
        )
    for cue_id in sorted(cue_keys - decided_cue):
        errors.append(
            DecisionError(
                cue_id=cue_id,
                type="missing",
                message=f"缺少 cue {cue_id} 的 cue 决策",
            )
        )

    if errors:
        return Resolution(errors=errors)

    # —— 应用全部决策，统一构造与校验 ——
    field_decisions = {(cue_id, fld): choice for cue_id, fld, choice in field_choices}
    cue_decisions = {cue_id: choice for cue_id, choice in cue_choices}

    base_map, cur_map, inc_map = _index(base), _index(current), _index(incoming)
    resolved: list[Cue] = list(merged)
    invalid_intervals: list[str] = []

    # cue 级冲突：delete_modify / add_add / invalid_interval
    for cc in cue_conflicts:
        choice = cue_decisions[cc.cue_id]
        if choice == "delete":
            continue
        survivor = cur_map.get(cc.cue_id) if choice == "current" else inc_map.get(cc.cue_id)
        if survivor is None:
            # 选择了已删除的一侧（delete_modify 中删除方）→ 等同删除
            continue
        # 来自请求版本的 cue 本身已通过校验，直接整条采用
        resolved.append(survivor.model_copy(deep=True))

    # 字段级冲突：无冲突字段按三方合并规则，冲突字段取所选一侧
    for cue_id in sorted(field_conflict_ids):
        b, c, i = base_map[cue_id], cur_map[cue_id], inc_map[cue_id]
        values: dict[str, object] = {"id": cue_id}
        for fld in MERGE_FIELDS:
            key = (cue_id, fld)
            if key in field_decisions:
                chosen = field_decisions[key]
                values[fld] = {
                    "base": getattr(b, fld),
                    "current": getattr(c, fld),
                    "incoming": getattr(i, fld),
                }[chosen]
                continue
            base_val, cur_val, inc_val = (
                getattr(b, fld),
                getattr(c, fld),
                getattr(i, fld),
            )
            cur_changed = cur_val != base_val
            inc_changed = inc_val != base_val
            if not cur_changed and not inc_changed:
                values[fld] = base_val
            elif cur_changed and not inc_changed:
                values[fld] = cur_val
            elif inc_changed and not cur_changed:
                values[fld] = inc_val
            else:
                values[fld] = cur_val  # 两侧改成相同值（该字段不在冲突集中）
        try:
            resolved.append(Cue(**values))
        except ValueError:
            invalid_intervals.append(cue_id)

    invalid_set = set(invalid_intervals)
    valid_cues = [cue for cue in resolved if cue.id not in invalid_set]
    overlaps = find_overlaps(valid_cues)
    valid_cues.sort(key=lambda cue: (cue.start, cue.end, cue.id))

    return Resolution(
        resolved=valid_cues,
        invalid_intervals=invalid_intervals,
        overlaps=overlaps,
    )


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
