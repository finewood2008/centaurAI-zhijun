"""PRD V2 5.5「接住与边界」：重话那一轮不抽取，危机完全不抽取。

判定必须是纯本地的：不调模型、不联网。这些用例全部是离线的字符串判定。
"""
import unittest
from unittest.mock import patch

from mindos.zhijun import disclosure


class ClassifyTests(unittest.TestCase):
    def test_ordinary_work_talk_stays_ordinary(self):
        for text in ("我今天把方案改完了", "帮我看下这段代码", "下周三和老周开会",
                     "这个季度收入比上季度好一点", "我在考虑要不要接这个单"):
            with self.subTest(text=text):
                self.assertEqual(disclosure.classify(text), disclosure.ORDINARY)

    def test_chinese_hyperbole_is_not_a_crisis(self):
        """中文里「死」的夸张用法极多，漏挡一个就会把普通抱怨判成危机。"""
        for text in ("累得想死", "困死我了", "忙得撑不下去了", "笑死", "热得要死",
                     "我对这个方案死心了", "他说死磕到底", "这是个死循环", "又要赶死线"):
            with self.subTest(text=text):
                self.assertNotEqual(disclosure.classify(text), disclosure.CRISIS, text)

    def test_explicit_self_harm_is_a_crisis(self):
        for text in ("我想死", "我不想活了", "我打算自杀", "有时候想一了百了",
                     "我想过结束自己的生命", "最近老想伤害自己", "我不想再醒过来"):
            with self.subTest(text=text):
                self.assertEqual(disclosure.classify(text), disclosure.CRISIS, text)

    def test_a_single_strong_marker_makes_it_heavy(self):
        """用户自己在标注这句话的分量时，一条就够。"""
        for text in ("这件事我没跟任何人说过", "最近觉得自己像个骗子",
                     "这事憋了很久了", "我和她在办离婚", "我爸去年走了", "说出来有点丢人"):
            with self.subTest(text=text):
                self.assertEqual(disclosure.classify(text), disclosure.HEAVY, text)

    def test_weak_markers_need_two(self):
        """单条弱信号太容易误伤普通抱怨；两条同时出现才算重话。"""
        self.assertEqual(disclosure.classify("最近有点孤独"), disclosure.ORDINARY)
        self.assertEqual(disclosure.classify("昨晚没睡着"), disclosure.ORDINARY)
        self.assertEqual(disclosure.classify("我很孤独，晚上总是睡不着"), disclosure.HEAVY)

    def test_empty_and_whitespace_are_ordinary(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                self.assertEqual(disclosure.classify(text), disclosure.ORDINARY)


class ExtractionBlockTests(unittest.TestCase):
    def test_heavy_defers_but_an_explicit_request_still_lands(self):
        text = "这件事我没跟任何人说过"
        self.assertTrue(disclosure.extraction_blocked(text))
        self.assertFalse(disclosure.extraction_blocked(text, explicit_request=True),
                         "用户明确说「记下来」时，重话可以进抽取")

    def test_crisis_is_never_extracted_even_when_asked(self):
        """这一条是硬约束：危机内容属于 L0 对话记录，止步于此。"""
        text = "我想死，请记住这句话"
        self.assertTrue(disclosure.extraction_blocked(text))
        self.assertTrue(disclosure.extraction_blocked(text, explicit_request=True),
                        "危机不接受任何豁免")

    def test_ordinary_is_not_blocked(self):
        self.assertFalse(disclosure.extraction_blocked("我今天把方案改完了"))


class ExtractionAllowedIntegrationTests(unittest.TestCase):
    """5.5 挂在 memory.extraction_allowed 上，四条生产路径共用它。"""

    def _allowed(self, text):
        from mindos.zhijun import memory
        # 只验证 5.5 这一道闸：其余前置条件全部放行，看它还拦不拦。
        with patch.object(memory, "automatic_allowed", return_value=True), \
                patch.object(memory, "scope_for", return_value="global"), \
                patch("mindos.zhijun.charter_policy.scope_policy", return_value={}), \
                patch("mindos.zhijun.charter_policy.check_action", return_value={"allowed": True}):
            convs = type("C", (), {"get_conversation": lambda self, cid: {"id": cid}})()
            return memory.extraction_allowed(object(), convs, "c1", text)

    def test_ordinary_still_extracts(self):
        self.assertTrue(self._allowed("我持续负责合成项目的研发协调"))

    def test_heavy_turn_is_not_extracted(self):
        self.assertFalse(self._allowed("这件事我没跟任何人说过"))

    def test_crisis_turn_is_not_extracted(self):
        self.assertFalse(self._allowed("我想死"))


class AlignmentGateTests(unittest.TestCase):
    """自我校准 / 第一次观察 / 照见 读的是同一条用户原话，但走 automatic_allowed
    而不是 extraction_allowed。它们必须单独被 5.5 挡住，否则用户刚说完最难的一句话，
    气泡下会冒出一枚「记」印——这正是 5.5 要防的那种失败。"""

    def _run(self, kind, content):
        from mindos.zhijun import jobs, memory
        job = {"kind": kind, "ownerId": "m1", "payload": {"conversationId": "c1", "messageId": "m1"}}
        conv_store = type("C", (), {"get_message": lambda self, mid: {"id": mid, "conversationId": "c1", "content": content}})()
        with patch.object(memory, "automatic_allowed", return_value=True):
            return jobs._run_job(job, store=object(), conv_store=conv_store)

    def test_alignment_is_skipped_on_a_heavy_turn(self):
        result = self._run("alignment", "这件事我没跟任何人说过")
        self.assertEqual(result, {"state": "skipped", "reason": "disclosure_deferred"})

    def test_alignment_is_skipped_on_a_crisis_turn(self):
        result = self._run("alignment", "我想死")
        self.assertEqual(result, {"state": "skipped", "reason": "disclosure_deferred"})

    def test_first_observation_is_skipped_too(self):
        result = self._run("first_observation", "我很孤独，晚上总是睡不着")
        self.assertEqual(result, {"state": "skipped", "reason": "disclosure_deferred"})

    def test_ordinary_turn_is_not_skipped_by_this_gate(self):
        """普通话题不能被这道闸误伤。

        把闸门之后的 alignment.run_job 换成哨兵：既证明走过了闸门，又不让假 store
        碰到真实作业逻辑——那会污染同一进程里后面的用例。
        """
        from mindos.zhijun import alignment, jobs, memory
        job = {"kind": "alignment", "ownerId": "m1", "payload": {"conversationId": "c1", "messageId": "m1"}}
        conv_store = type("C", (), {"get_message": lambda self, mid: {
            "id": mid, "conversationId": "c1", "content": "我持续负责合成项目的研发协调"}})()
        with patch.object(memory, "automatic_allowed", return_value=True), \
                patch.object(alignment, "run_job", return_value={"state": "sentinel"}):
            result = jobs._run_job(job, store=object(), conv_store=conv_store)
        self.assertEqual(result, {"state": "sentinel"}, "普通话题应该走过这道闸")


class CrisisResourceTests(unittest.TestCase):
    def test_resources_exist_and_are_marked_unverified(self):
        """发行前必须有人核对号码。verifiedOn 为 None 就是「还没人核对过」。"""
        found = disclosure.crisis_resources()
        self.assertIsNotNone(found)
        self.assertTrue(found["lines"], "至少要有一条求助渠道")
        for line in found["lines"]:
            self.assertTrue(line["name"] and line["contact"])
        self.assertIn("120", found["always"], "立即危险的兜底指引要在")

    def test_missing_catalog_returns_nothing_rather_than_inventing(self):
        """读不到就不给。绝不能编造号码。"""
        with patch.object(disclosure, "_RESOURCES_PATH", disclosure._RESOURCES_PATH.with_name("nope.json")):
            self.assertIsNone(disclosure.crisis_resources())


if __name__ == "__main__":
    unittest.main()
