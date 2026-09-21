"""字幕三方合并 API 的端到端测试（标准库 unittest + FastAPI TestClient）。"""

import unittest

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def cue(id, start, end, text="t", speaker=None):
    data = {"id": id, "start": start, "end": end, "text": text}
    if speaker is not None:
        data["speaker"] = speaker
    return data


class HealthTests(unittest.TestCase):
    def test_health(self):
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})


class MergeTests(unittest.TestCase):
    def test_unchanged_three_versions_merge_clean(self):
        body = {
            "base": [cue("a", 0, 2, "hello")],
            "current": [cue("a", 0, 2, "hello")],
            "incoming": [cue("a", 0, 2, "hello")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["status"], "merged")
        self.assertEqual(data["count"], 1)

    def test_single_side_change_is_adopted(self):
        # current 单独修改文本；incoming 未动
        body = {
            "base": [cue("a", 0, 2, "hello")],
            "current": [cue("a", 0, 2, "hello!")],
            "incoming": [cue("a", 0, 2, "hello")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["cues"][0]["text"], "hello!")

    def test_both_sides_change_different_fields(self):
        # current 改时间，incoming 改文本：各自采纳
        body = {
            "base": [cue("a", 0, 2, "hello")],
            "current": [cue("a", 1, 3, "hello")],
            "incoming": [cue("a", 0, 2, "你好")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        merged = resp.json()["cues"][0]
        self.assertEqual((merged["start"], merged["end"]), (1, 3))
        self.assertEqual(merged["text"], "你好")

    def test_both_sides_change_to_same_value(self):
        body = {
            "base": [cue("a", 0, 2, "hello")],
            "current": [cue("a", 0, 2, "hi")],
            "incoming": [cue("a", 0, 2, "hi")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["cues"][0]["text"], "hi")

    def test_field_level_conflict_different_values(self):
        body = {
            "base": [cue("a", 0, 2, "hello")],
            "current": [cue("a", 0, 2, "hi")],
            "incoming": [cue("a", 0, 2, "yo")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertEqual(data["status"], "conflict")
        self.assertEqual(len(data["field_conflicts"]), 1)
        fc = data["field_conflicts"][0]
        self.assertEqual(fc["cue_id"], "a")
        self.assertEqual(fc["field"], "text")
        self.assertEqual(fc["base"], "hello")
        self.assertEqual(fc["current"], "hi")
        self.assertEqual(fc["incoming"], "yo")

    def test_both_delete_is_clean(self):
        body = {
            "base": [cue("a", 0, 2), cue("b", 3, 4)],
            "current": [cue("b", 3, 4)],
            "incoming": [cue("b", 3, 4)],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual([c["id"] for c in resp.json()["cues"]], ["b"])

    def test_delete_vs_modify_is_conflict(self):
        # current 删除 a，incoming 修改 a
        body = {
            "base": [cue("a", 0, 2, "hello")],
            "current": [],
            "incoming": [cue("a", 0, 2, "hello!")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertEqual(data["status"], "conflict")
        self.assertEqual(len(data["cue_conflicts"]), 1)
        cc = data["cue_conflicts"][0]
        self.assertEqual(cc["cue_id"], "a")
        self.assertEqual(cc["type"], "delete_modify")
        self.assertEqual(cc["side"], "current")
        self.assertIn("text", cc["fields"])

    def test_delete_vs_unchanged_adopts_delete(self):
        # 一侧删除，另一侧原样保留：删除生效
        body = {
            "base": [cue("a", 0, 2, "hello")],
            "current": [],
            "incoming": [cue("a", 0, 2, "hello")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["count"], 0)

    def test_add_add_same_value_adopts(self):
        body = {
            "base": [],
            "current": [cue("n", 5, 7, "new")],
            "incoming": [cue("n", 5, 7, "new")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["cues"][0]["id"], "n")

    def test_add_add_different_value_is_conflict(self):
        body = {
            "base": [],
            "current": [cue("n", 5, 7, "new")],
            "incoming": [cue("n", 5, 7, "brand new")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 409)
        cc = resp.json()["cue_conflicts"][0]
        self.assertEqual(cc["type"], "add_add")
        self.assertEqual(cc["fields"], ["text"])

    def test_result_sorted_by_start(self):
        body = {
            "base": [],
            "current": [cue("c", 10, 11, "late"), cue("a", 0, 1, "early")],
            "incoming": [cue("b", 5, 6, "mid")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = [c["id"] for c in resp.json()["cues"]]
        self.assertEqual(ids, ["a", "b", "c"])

    def test_overlap_reported_without_merged_output(self):
        # 无冲突，但 current 新增的 cue 与 incoming 新增的 cue 时间重叠
        body = {
            "base": [],
            "current": [cue("a", 0, 5, "first")],
            "incoming": [cue("b", 3, 8, "second")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertEqual(data["status"], "overlap")
        self.assertNotIn("cues", data)
        self.assertEqual(len(data["overlaps"]), 1)
        ov = data["overlaps"][0]
        self.assertEqual(ov["cue_ids"], ["a", "b"])
        self.assertAlmostEqual(ov["start"], 3)
        self.assertAlmostEqual(ov["end"], 5)
        self.assertAlmostEqual(ov["overlap"], 2)

    def test_touching_intervals_are_not_overlap(self):
        # 首尾相接（end == start）不算重叠
        body = {
            "base": [],
            "current": [cue("a", 0, 5, "first")],
            "incoming": [cue("b", 5, 8, "second")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_conflict_blocks_overlap_check(self):
        # 同时存在冲突和潜在重叠：报告冲突，不进入重叠检测
        body = {
            "base": [cue("a", 0, 2, "x")],
            "current": [cue("a", 0, 2, "one")],
            "incoming": [cue("a", 0, 2, "two")],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["status"], "conflict")

    def test_invalid_interval_rejected(self):
        body = {
            "base": [cue("a", 0, 2)],
            "current": [cue("a", 5, 2)],
            "incoming": [cue("a", 0, 2)],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 422)

    def test_duplicate_id_within_one_version_rejected(self):
        body = {
            "base": [cue("a", 0, 2), cue("a", 3, 4)],
            "current": [],
            "incoming": [],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 422)

    def test_speaker_none_equivalence(self):
        # base 未提供 speaker 与一侧显式给出 speaker: null 视为未修改
        body = {
            "base": [{"id": "a", "start": 0, "end": 2, "text": "x"}],
            "current": [
                {"id": "a", "start": 0, "end": 2, "text": "x", "speaker": None}
            ],
            "incoming": [
                {"id": "a", "start": 0, "end": 2, "text": "x", "speaker": "bob"}
            ],
        }
        resp = client.post("/merge", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["cues"][0]["speaker"], "bob")


class ResolveTests(unittest.TestCase):
    # —— 无冲突：仅允许空决策 ——

    def test_no_conflict_empty_decisions_resolves(self):
        body = {
            "base": [cue("a", 0, 2, "hello")],
            "current": [cue("a", 0, 2, "hello!")],
            "incoming": [cue("a", 0, 2, "hello")],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["status"], "resolved")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["cues"][0]["text"], "hello!")

    def test_no_conflict_empty_lists_resolves(self):
        resp = client.post(
            "/resolve",
            json={
                "base": [cue("a", 0, 2)],
                "current": [cue("a", 0, 2)],
                "incoming": [cue("a", 0, 2)],
                "field_choices": [],
                "cue_choices": [],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "resolved")

    def test_no_conflict_with_field_choice_is_unknown(self):
        body = {
            "base": [cue("a", 0, 2)],
            "current": [cue("a", 0, 2)],
            "incoming": [cue("a", 0, 2)],
            "field_choices": [
                {"cue_id": "a", "field": "text", "choice": "current"}
            ],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        data = resp.json()
        self.assertEqual(data["status"], "decision_error")
        self.assertEqual(len(data["errors"]), 1)
        self.assertEqual(data["errors"][0]["type"], "unknown")

    def test_no_conflict_with_cue_choice_is_unknown(self):
        body = {
            "base": [cue("a", 0, 2)],
            "current": [cue("a", 0, 2)],
            "incoming": [cue("a", 0, 2)],
            "cue_choices": [{"cue_id": "a", "choice": "delete"}],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["errors"][0]["type"], "unknown")

    # —— 字段级冲突 ——

    def test_field_conflict_resolve_pick_side(self):
        for choice, expected in [
            ("base", "hi"),
            ("current", "one"),
            ("incoming", "two"),
        ]:
            with self.subTest(choice=choice):
                body = {
                    "base": [cue("a", 0, 2, "hi")],
                    "current": [cue("a", 0, 2, "one")],
                    "incoming": [cue("a", 0, 2, "two")],
                    "field_choices": [
                        {"cue_id": "a", "field": "text", "choice": choice}
                    ],
                }
                resp = client.post("/resolve", json=body)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["cues"][0]["text"], expected)

    def test_field_choice_merges_non_conflicting_fields(self):
        # current 改文本，incoming 改文本（冲突）与起点；起点单侧改动仍自动采纳
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [cue("a", 0, 2, "one")],
            "incoming": [cue("a", 1, 2, "two")],
            "field_choices": [
                {"cue_id": "a", "field": "text", "choice": "current"}
            ],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        resolved = resp.json()["cues"][0]
        self.assertEqual(resolved["text"], "one")
        self.assertEqual(resolved["start"], 1)

    def test_multiple_field_conflicts_on_same_cue(self):
        body = {
            "base": [cue("a", 0, 10, "keep")],
            "current": [cue("a", 8, 9, "cur")],
            "incoming": [cue("a", 1, 3, "inc")],
            "field_choices": [
                {"cue_id": "a", "field": "start", "choice": "incoming"},
                {"cue_id": "a", "field": "end", "choice": "current"},
                {"cue_id": "a", "field": "text", "choice": "base"},
            ],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        cue_out = resp.json()["cues"][0]
        self.assertEqual((cue_out["start"], cue_out["end"], cue_out["text"]), (1, 9, "keep"))

    def test_missing_field_choice_is_422(self):
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [cue("a", 0, 2, "one")],
            "incoming": [cue("a", 0, 2, "two")],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        errors = resp.json()["errors"]
        self.assertTrue(any(e["type"] == "missing" for e in errors))

    def test_missing_multiple_field_choices_all_reported(self):
        body = {
            "base": [cue("a", 0, 10, "keep")],
            "current": [cue("a", 8, 9, "cur")],
            "incoming": [cue("a", 1, 3, "inc")],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        missing = {e["field"] for e in resp.json()["errors"] if e["type"] == "missing"}
        self.assertEqual(missing, {"start", "end", "text"})

    def test_unknown_field_choice_is_422(self):
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [cue("a", 0, 2, "one")],
            "incoming": [cue("a", 0, 2, "two")],
            "field_choices": [
                {"cue_id": "a", "field": "text", "choice": "current"},
                {"cue_id": "a", "field": "start", "choice": "current"},
            ],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        errors = resp.json()["errors"]
        unknown = [e for e in errors if e["type"] == "unknown"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["field"], "start")

    def test_unknown_cue_id_in_field_choice_is_422(self):
        body = {
            "base": [cue("a", 0, 2)],
            "current": [cue("a", 0, 2)],
            "incoming": [cue("a", 0, 2)],
            "field_choices": [
                {"cue_id": "ghost", "field": "text", "choice": "base"}
            ],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["errors"][0]["type"], "unknown")

    def test_duplicate_field_choice_is_422(self):
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [cue("a", 0, 2, "one")],
            "incoming": [cue("a", 0, 2, "two")],
            "field_choices": [
                {"cue_id": "a", "field": "text", "choice": "current"},
                {"cue_id": "a", "field": "text", "choice": "incoming"},
            ],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        types = [e["type"] for e in resp.json()["errors"]]
        self.assertEqual(types, ["duplicate"])

    def test_field_choice_on_cue_conflict_is_type_mismatch(self):
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [],
            "incoming": [cue("a", 0, 2, "two")],
            "field_choices": [
                {"cue_id": "a", "field": "text", "choice": "incoming"}
            ],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        errors = resp.json()["errors"]
        self.assertTrue(any(e["type"] == "type_mismatch" for e in errors))
        self.assertTrue(any(e["type"] == "missing" and e["field"] is None for e in errors))

    # —— cue 级冲突 ——

    def test_delete_modify_pick_incoming(self):
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [],
            "incoming": [cue("a", 0, 2, "two")],
            "cue_choices": [{"cue_id": "a", "choice": "incoming"}],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        cues = resp.json()["cues"]
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "two")

    def test_delete_modify_pick_deleted_side_deletes(self):
        # current 已删除，选择 current 等同删除
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [],
            "incoming": [cue("a", 0, 2, "two")],
            "cue_choices": [{"cue_id": "a", "choice": "current"}],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["count"], 0)

    def test_delete_modify_choice_delete(self):
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [],
            "incoming": [cue("a", 0, 2, "two")],
            "cue_choices": [{"cue_id": "a", "choice": "delete"}],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["count"], 0)

    def test_add_add_pick_each_side(self):
        for choice, expected in [("current", "new"), ("incoming", "brand")]:
            with self.subTest(choice=choice):
                body = {
                    "base": [],
                    "current": [cue("n", 5, 7, "new")],
                    "incoming": [cue("n", 5, 7, "brand")],
                    "cue_choices": [{"cue_id": "n", "choice": choice}],
                }
                resp = client.post("/resolve", json=body)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["cues"][0]["text"], expected)

    def test_invalid_interval_cue_conflict_pick_side(self):
        # current 只改起点、incoming 只改终点，自动合并为 [8,3] 非法
        body = {
            "base": [cue("a", 0, 10)],
            "current": [cue("a", 8, 10)],
            "incoming": [cue("a", 0, 3)],
        }
        conflict = client.post("/merge", json=body).json()
        self.assertEqual(conflict["cue_conflicts"][0]["type"], "invalid_interval")

        resolved_body = dict(body)
        resolved_body["cue_choices"] = [{"cue_id": "a", "choice": "current"}]
        resp = client.post("/resolve", json=resolved_body)
        self.assertEqual(resp.status_code, 200, resp.text)
        out = resp.json()["cues"][0]
        self.assertEqual((out["start"], out["end"]), (8, 10))

    def test_missing_cue_choice_is_422(self):
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [],
            "incoming": [cue("a", 0, 2, "two")],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        errors = resp.json()["errors"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "missing")
        self.assertEqual(errors[0]["cue_id"], "a")

    def test_unknown_cue_choice_is_422(self):
        body = {
            "base": [cue("a", 0, 2)],
            "current": [cue("a", 0, 2)],
            "incoming": [cue("a", 0, 2)],
            "cue_choices": [{"cue_id": "ghost", "choice": "incoming"}],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["errors"][0]["type"], "unknown")

    def test_duplicate_cue_choice_is_422(self):
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [],
            "incoming": [cue("a", 0, 2, "two")],
            "cue_choices": [
                {"cue_id": "a", "choice": "delete"},
                {"cue_id": "a", "choice": "incoming"},
            ],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual([e["type"] for e in resp.json()["errors"]], ["duplicate"])

    def test_cue_choice_on_field_conflict_is_type_mismatch(self):
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [cue("a", 0, 2, "one")],
            "incoming": [cue("a", 0, 2, "two")],
            "cue_choices": [{"cue_id": "a", "choice": "delete"}],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)
        errors = resp.json()["errors"]
        self.assertTrue(any(e["type"] == "type_mismatch" and e["field"] is None for e in errors))

    def test_invalid_choice_value_is_422(self):
        body = {
            "base": [cue("a", 0, 2, "hi")],
            "current": [],
            "incoming": [cue("a", 0, 2, "two")],
            "cue_choices": [{"cue_id": "a", "choice": "base"}],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 422)

    # —— 应用决策后的统一校验（409，不输出部分结果）——

    def test_field_decisions_producing_invalid_interval_409(self):
        body = {
            "base": [cue("a", 0, 10, "keep")],
            "current": [cue("a", 8, 9, "cur")],
            "incoming": [cue("a", 1, 3, "inc")],
            "field_choices": [
                {"cue_id": "a", "field": "start", "choice": "current"},
                {"cue_id": "a", "field": "end", "choice": "incoming"},
                {"cue_id": "a", "field": "text", "choice": "base"},
            ],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertEqual(data["status"], "timeline_error")
        self.assertEqual(data["invalid_intervals"], ["a"])
        self.assertEqual(data["overlaps"], [])
        self.assertNotIn("cues", data)

    def test_field_decisions_producing_overlap_409(self):
        # a、b 各自字段冲突；选 incoming 侧时间后 a=[1,4]、b=[3,12] 重叠
        body = {
            "base": [cue("a", 0, 10, "x"), cue("b", 10, 12, "y")],
            "current": [cue("a", 8, 9, "cx"), cue("b", 9, 12, "cy")],
            "incoming": [cue("a", 1, 4, "ix"), cue("b", 3, 12, "iy")],
            "field_choices": [
                {"cue_id": "a", "field": "start", "choice": "incoming"},
                {"cue_id": "a", "field": "end", "choice": "incoming"},
                {"cue_id": "a", "field": "text", "choice": "incoming"},
                {"cue_id": "b", "field": "start", "choice": "incoming"},
                {"cue_id": "b", "field": "text", "choice": "incoming"},
            ],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertEqual(data["status"], "timeline_error")
        self.assertEqual(data["invalid_intervals"], [])
        self.assertEqual(len(data["overlaps"]), 1)
        self.assertEqual(data["overlaps"][0]["cue_ids"], ["a", "b"])

    def test_touching_intervals_after_decisions_resolve(self):
        # 同样的冲突，选 current 侧：a=[8,9]、b=[9,12]，首尾相接合法
        body = {
            "base": [cue("a", 0, 10, "x"), cue("b", 10, 12, "y")],
            "current": [cue("a", 8, 9, "cx"), cue("b", 9, 12, "cy")],
            "incoming": [cue("a", 1, 4, "ix"), cue("b", 3, 12, "iy")],
            "field_choices": [
                {"cue_id": "a", "field": "start", "choice": "current"},
                {"cue_id": "a", "field": "end", "choice": "current"},
                {"cue_id": "a", "field": "text", "choice": "current"},
                {"cue_id": "b", "field": "start", "choice": "current"},
                {"cue_id": "b", "field": "text", "choice": "current"},
            ],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = [c["id"] for c in resp.json()["cues"]]
        self.assertEqual(ids, ["a", "b"])

    def test_no_conflict_but_overlap_still_409(self):
        body = {
            "base": [],
            "current": [cue("a", 0, 5, "first")],
            "incoming": [cue("b", 3, 8, "second")],
        }
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertEqual(data["status"], "timeline_error")
        self.assertEqual(len(data["overlaps"]), 1)

    def test_resolved_timeline_is_sorted_by_time(self):
        # cue 级冲突与干净合并同时存在，结果统一按时间排序
        body = {
            "base": [
                cue("a", 0, 2, "hi"),
                cue("d", 20, 21, "keep"),
            ],
            "current": [cue("d", 20, 21, "keep")],
            "incoming": [
                cue("a", 0, 2, "two"),
                cue("d", 20, 21, "keep"),
                cue("c", 10, 11, "late"),
            ],
            "cue_choices": [{"cue_id": "a", "choice": "incoming"}],
        }
        # 再让 current 单独新增一条更早的 cue
        body["current"].append(cue("b", 5, 6, "mid"))
        resp = client.post("/resolve", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = [c["id"] for c in resp.json()["cues"]]
        self.assertEqual(ids, ["a", "b", "c", "d"])


if __name__ == "__main__":
    unittest.main()
