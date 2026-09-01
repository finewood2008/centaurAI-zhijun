# -*- coding: utf-8 -*-
"""临时验证 derived 关系函数（运行后即删）。"""
from mindos import derived as d

# 1) 谓词归一化
assert d._normalize_relation_predicate("替代") == "替代"
assert d._normalize_relation_predicate("取代") == "替代"
assert d._normalize_relation_predicate("属于") == "属于"
assert d._normalize_relation_predicate("根本不成立") is None
print("pred_normalize ok")

# 2) LLM JSON 解析：端点必须命中实体产物，谓词同义映射，type 以实体为准
ei = {"A": {"type": "term", "name": "A"}, "B": {"type": "person", "name": "B"}}
r = d._parse_relation_json(
    '[{"subject":{"name":"A"},"predicate":"取代","object":{"name":"B"},"confidence":0.8}]',
    ei, "今天我们用A取代了B方法"
)
assert r is not None and len(r) == 1, r
rel = r[0]
assert rel["predicate"] == "替代", rel
assert rel["subject"]["type"] == "term" and rel["object"]["type"] == "person", rel
assert rel["confidence"] == 0.8
assert rel["evidence"] and "A" in rel["evidence"], rel
print("parse ok:", rel["predicate"], rel["subject"]["type"], rel["object"]["type"])

# 3) 端点不在实体产物内 → 丢弃
r = d._parse_relation_json(
    '[{"subject":{"name":"X"},"predicate":"替代","object":{"name":"B"},"confidence":0.9}]',
    ei, "X替代B"
)
assert r is None, r  # 全部幻觉
print("dropped-unknown-entity ok")

# 4) 空数组 → 合法空
assert d._parse_relation_json("[]", ei, "x") == []
print("empty-ok")

# 5) 非法 confidence / 谓词 → 丢弃（返回 None）
r = d._parse_relation_json(
    '[{"subject":{"name":"A"},"predicate":"替代","object":{"name":"B"},"confidence":2.5}]',
    ei, "A替代B"
)
assert r is None, r
print("bad-confidence dropped")

# 6) fallback：白名单谓词字面共现 + 双侧实体 → 产出
fb = d._relation_fallback("新方案A替代了旧方案B", ["A", "B"])
assert len(fb) == 1 and fb[0]["predicate"] == "替代", fb
print("fallback ok:", fb[0]["subject"]["name"], fb[0]["predicate"], fb[0]["object"]["name"])

# 7) fallback：无谓词 / 单侧实体 / 对象非实体 → 不产出
assert d._relation_fallback("本文属于内部材料", ["本文"]) == []
assert d._relation_fallback("这是一段普通文字", ["A", "B"]) == []
print("fallback-no-noise ok")

# 8) fallback 同义词
fb2 = d._relation_fallback("团队采用了A并且后来使用B做了对比", ["A", "B"])
# 应命中"采用/使用"或"对比"；只要有产出且谓词在白名单即可
print("fallback-synonym sample:", [x["predicate"] for x in fb2])

print("ALL_RELATION_CHECKS_PASS")