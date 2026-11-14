import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logic import (  # noqa: E402
    chunk_lines,
    effective_allowed,
    parse_index,
    resolve_target,
    summarize_prompt,
    unique_preserve,
)


class LogicTests(unittest.TestCase):
    def test_unique_preserve(self):
        self.assertEqual(unique_preserve(["a", "b", "a", "", "b"]), ["a", "b", ""])

    def test_effective_allowed_all(self):
        self.assertEqual(effective_allowed(["a", "b"], ["a"], True), ["a", "b"])

    def test_effective_allowed_filter(self):
        self.assertEqual(effective_allowed(["a", "b", "c"], ["c", "a"], False), ["a", "c"])

    def test_effective_allowed_empty(self):
        self.assertEqual(effective_allowed(["a"], None, False), [])

    def test_resolve_exact(self):
        self.assertEqual(resolve_target("猫娘", ["default", "猫娘"]).persona_id, "猫娘")

    def test_resolve_case_insensitive(self):
        self.assertEqual(resolve_target("NEKO", ["neko", "other"]).persona_id, "neko")

    def test_resolve_substring(self):
        self.assertEqual(resolve_target("猫", ["default", "猫娘", "狗"]).persona_id, "猫娘")

    def test_resolve_ambiguous(self):
        result = resolve_target("猫", ["猫娘", "猫咪"])
        self.assertIsNone(result.persona_id)
        self.assertEqual(len(result.ambiguous), 2)

    def test_resolve_missing(self):
        result = resolve_target("不存在", ["a"])
        self.assertIsNone(result.persona_id)
        self.assertEqual(result.error, "没有找到这个人格")

    def test_parse_index(self):
        self.assertEqual(parse_index("2", ["a", "b"]), "b")
        self.assertIsNone(parse_index("0", ["a"]))
        self.assertIsNone(parse_index("3", ["a", "b"]))
        self.assertIsNone(parse_index("x", ["a"]))

    def test_chunk_lines(self):
        self.assertEqual(chunk_lines(["a", "b", "c"], 2), (["a", "b"], 1))
        self.assertEqual(chunk_lines(["a"], 2), (["a"], 0))

    def test_summarize_prompt(self):
        self.assertEqual(summarize_prompt("\n\n第一行\n第二行"), "第一行")
        self.assertEqual(summarize_prompt("x" * 80, 10), "x" * 10 + "…")
        self.assertEqual(summarize_prompt(None), "")


if __name__ == "__main__":
    unittest.main()
