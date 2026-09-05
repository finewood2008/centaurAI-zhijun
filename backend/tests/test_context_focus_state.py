"""Working situations survive refresh without becoming permanent profile facts."""
import unittest
from mindos.zhijun.memory_context import build_focus


class WorkingFocusTests(unittest.TestCase):
    def history(self):
        initial = "星桥项目的新同学下周才入职，还没有接触资料，只承担辅助工作。"
        focus = build_focus(initial, [])
        return [{"id": "u1", "role": "user", "content": initial},
                {"id": "a1", "role": "assistant", "content": "你担心的是他接触到哪一部分？",
                 "meta": {"replyTo": "u1", "routingProvenance": {"contextPlan": {"focus": focus}}}}]

    def test_refresh_and_multiple_short_answers_keep_original_conditions(self):
        history = self.history()
        for index, text in enumerate(["核心方案", "只是辅助性的角色", "对，就是担心这个", "那应该怎么办", "对，就是这个"]):
            focus = build_focus(text, history[-4:])
            self.assertIn("下周才入职", focus["event"])
            self.assertIn("还没有接触资料", focus["event"])
            user_id = f"next-user-{index}"
            history.extend([{"id": user_id, "role": "user", "content": text},
                {"id": f"next-assistant-{index}", "role": "assistant", "content": "先限制核心方案的接触范围。",
                 "meta": {"replyTo": user_id, "routingProvenance": {"contextPlan": {"focus": focus}}}}])

    def test_topic_switch_drops_the_old_event_and_its_dependencies(self):
        focus = build_focus("换个话题，我明天去海边需要带什么？", self.history())
        self.assertTrue(focus["topicChanged"])
        self.assertEqual(focus["historyUsed"], [])
        self.assertNotIn("星桥", focus["query"])
        self.assertEqual(len(focus["userStatements"]), 1)

    def test_question_is_never_promoted_to_user_fact(self):
        history = self.history()
        history[-1]["content"] = "你已经让他读取全部核心代码了吗？"
        focus = build_focus("没有", history)
        self.assertIn("全部核心代码", focus["question"])
        self.assertFalse(any("全部核心代码" in r["text"] for r in focus["userStatements"]))
        self.assertTrue(any("还没有接触资料" in r["text"] for r in focus["userStatements"]))
