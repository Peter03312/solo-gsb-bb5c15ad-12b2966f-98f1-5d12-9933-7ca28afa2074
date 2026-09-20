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


if __name__ == "__main__":
    unittest.main()
