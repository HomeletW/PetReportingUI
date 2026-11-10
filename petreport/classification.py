"""Consolidate model findings for the whole visit, including older saved reports."""


def consolidate_classification(classification):
    if not classification:
        return classification
    by_behavior = {}
    for item in classification["observations"]:
        by_behavior.setdefault(item["behavior"], []).append(item)
    observations = []
    for group in by_behavior.values():
        representative = max(group, key=lambda item: len(set(item["evidence_ids"])))
        evidence = list(dict.fromkeys(mid for item in group for mid in item["evidence_ids"]))
        finding = {**representative, "evidence_ids": evidence[:20]}
        if len({item["assessment"] for item in group}) > 1:
            finding.update(assessment="uncertain", reason="同一组截图的判断不一致，暂不确认该行为。")
        observations.append(finding)
    return {**classification, "observations": observations}
