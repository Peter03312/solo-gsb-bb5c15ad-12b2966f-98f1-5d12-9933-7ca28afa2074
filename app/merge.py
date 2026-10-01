"""按 cue id 执行三方合并的纯逻辑层。

合并规则（相对 ``base`` 共同祖先）：

* 只有一侧修改了某字段 → 采纳该侧的值；
* 两侧都未修改 → 保留 base 的值；
* 两侧都改成了相同的值 → 采纳该值；
* 两侧改成不同的值 → 记录字段级冲突；
* 一侧删除、另一侧修改 → 记录 cue 级冲突（delete_modify）；
* 两侧都删除 → 干净删除；
* 两侧各自新增同一 id：取值一致则采纳，不一致记录 add_add 冲突。

仅当不存在任何冲突时才检查时间轴：合并结果按开始时间排序后，
若存在区间重叠（区间相交且重叠时长大于 0，仅首尾相接不算重叠），
则报告所有重叠的 cue id 及重叠区间，不输出合并版本。
"""

from __future__ import annotations

from typing import Optional

from pydantic import ValidationError

from .models import (
    MERGE_FIELDS,
    Cue,
    CueChoice,
    CueConflict,
    FieldChoice,
    FieldConflict,
    Overlap,
    ResolveDecisionError,
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


def _merge_surviving_fields(
    cue_id: str,
    b: Cue,
    c: Cue,
    i: Cue,
) -> tuple[dict[str, object], list[FieldConflict]]:
    """两侧都存活时逐字段合并，返回 (取值表, 字段级冲突)。

    冲突字段不会写入取值表，交由调用方（自动合并或人工决策）决定。
    """
    values: dict[str, object] = {"id": cue_id}
    field_conflicts: list[FieldConflict] = []
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
    return values, field_conflicts


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
            values, cue_field_conflicts = _merge_surviving_fields(cue_id, b, c, i)
            field_conflicts.extend(cue_field_conflicts)

            if not cue_field_conflicts:
                try:
                    merged.append(Cue(**values))
                except ValidationError:
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


def validate_decisions(
    cue_conflicts: list[CueConflict],
    field_conflicts: list[FieldConflict],
    field_choices: list[FieldChoice],
    cue_choices: list[CueChoice],
) -> tuple[list[ResolveDecisionError], dict[tuple[str, str], str], dict[str, str]]:
    """校验人工决策是否恰好覆盖全部冲突。

    返回 (错误列表, 字段决策映射 (cue_id, field) -> side, cue 决策映射 cue_id -> side)。
    错误类型：

    * duplicate：同一冲突点被重复决策；
    * unknown：决策指向不存在冲突的 cue_id（或 cue_id + field）；
    * type_mismatch：冲突类型与决策类型不符（字段冲突放进了 cue_choices 或反之）；
    * missing：存在冲突却没有对应决策。
    """
    errors: list[ResolveDecisionError] = []

    field_required: dict[tuple[str, str], FieldConflict] = {
        (fc.cue_id, fc.field): fc for fc in field_conflicts
    }
    cue_required: dict[str, CueConflict] = {cc.cue_id: cc for cc in cue_conflicts}

    field_decisions: dict[tuple[str, str], str] = {}
    cue_decisions: dict[str, str] = {}

    for fc in field_choices:
        key = (fc.cue_id, fc.field)
        if key in field_decisions:
            errors.append(
                ResolveDecisionError(
                    type="duplicate",
                    cue_id=fc.cue_id,
                    field=fc.field,
                    message=f"字段 {fc.cue_id}.{fc.field} 的决策重复",
                )
            )
            continue
        if fc.cue_id in cue_required:
            errors.append(
                ResolveDecisionError(
                    type="type_mismatch",
                    cue_id=fc.cue_id,
                    field=fc.field,
                    message=f"cue {fc.cue_id} 是 cue 级冲突（{cue_required[fc.cue_id].type}），"
                    "不能在 field_choices 中决策",
                )
            )
            continue
        if key not in field_required:
            errors.append(
                ResolveDecisionError(
                    type="unknown",
                    cue_id=fc.cue_id,
                    field=fc.field,
                    message=f"不存在字段冲突 {fc.cue_id}.{fc.field}",
                )
            )
            continue
        field_decisions[key] = fc.choice

    for cc in cue_choices:
        if cc.cue_id in cue_decisions:
            errors.append(
                ResolveDecisionError(
                    type="duplicate",
                    cue_id=cc.cue_id,
                    message=f"cue {cc.cue_id} 的决策重复",
                )
            )
            continue
        if cc.cue_id in cue_required:
            cue_decisions[cc.cue_id] = cc.choice
            continue
        if cc.cue_id in {fc.cue_id for fc in field_conflicts}:
            errors.append(
                ResolveDecisionError(
                    type="type_mismatch",
                    cue_id=cc.cue_id,
                    message=f"cue {cc.cue_id} 只有字段级冲突，必须在 field_choices 中逐字段决策",
                )
            )
        else:
            errors.append(
                ResolveDecisionError(
                    type="unknown",
                    cue_id=cc.cue_id,
                    message=f"不存在 cue 级冲突 {cc.cue_id}",
                )
            )

    for key in field_required:
        if key not in field_decisions and not any(
            err.cue_id == key[0] and err.field == key[1] for err in errors
        ):
            errors.append(
                ResolveDecisionError(
                    type="missing",
                    cue_id=key[0],
                    field=key[1],
                    message=f"缺少字段冲突 {key[0]}.{key[1]} 的决策",
                )
            )
    for cue_id in cue_required:
        if cue_id not in cue_decisions and not any(
            err.cue_id == cue_id for err in errors
        ):
            errors.append(
                ResolveDecisionError(
                    type="missing",
                    cue_id=cue_id,
                    message=f"缺少 cue 级冲突 {cue_id}（{cue_required[cue_id].type}）的决策",
                )
            )

    errors.sort(key=lambda err: (err.type, err.cue_id, err.field or ""))
    return errors, field_decisions, cue_decisions


def apply_decisions(
    base: list[Cue],
    current: list[Cue],
    incoming: list[Cue],
    cue_conflicts: list[CueConflict],
    field_conflicts: list[FieldConflict],
    field_decisions: dict[tuple[str, str], str],
    cue_decisions: dict[str, str],
) -> tuple[list[Cue], list[str]]:
    """在自动合并结果之上应用全部人工决策。

    返回 (应用决策后未排序且未校验时间轴的 cues, 构造后 end <= start 的 cue id)。
    调用方须保证决策已通过 :func:`validate_decisions` 校验。
    """
    merged, _, _ = three_way_merge(base, current, incoming)
    base_map, cur_map, inc_map = _index(base), _index(current), _index(incoming)

    # cue 级冲突：整条采纳一侧版本或删除
    for cc in cue_conflicts:
        side = cue_decisions[cc.cue_id]
        if side == "delete":
            continue
        chosen = {"current": cur_map.get(cc.cue_id), "incoming": inc_map.get(cc.cue_id)}[side]
        if chosen is not None:
            merged.append(chosen.model_copy(deep=True))

    # 字段级冲突：非冲突字段照常自动合并，冲突字段按决策取对应侧的值
    field_conflict_ids = list(dict.fromkeys(fc.cue_id for fc in field_conflicts))
    invalid_interval_ids: list[str] = []
    for cue_id in field_conflict_ids:
        b, c, i = base_map[cue_id], cur_map[cue_id], inc_map[cue_id]
        values, _ = _merge_surviving_fields(cue_id, b, c, i)
        for key, side in field_decisions.items():
            if key[0] != cue_id:
                continue
            field = key[1]
            values[field] = getattr(
                {"base": b, "current": c, "incoming": i}[side], field
            )
        try:
            merged.append(Cue(**values))
        except ValidationError:
            invalid_interval_ids.append(cue_id)

    return merged, invalid_interval_ids


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
