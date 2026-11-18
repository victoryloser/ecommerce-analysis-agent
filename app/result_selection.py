"""Apply explicit display requests to the effective contribution plan."""
import re

from app.grouping import dimensions
from app.semantic import PlanError


GROUPS = {"类": "category", "品类": "category", "类别": "category", "商家": "seller",
          "省份": "customer_state", "地区": "customer_state", "分组": None, "组合": None}
GROUP = r"品类|类别|商家|省份|地区|分组|组合"
DISPLAY = r"列出|展示|显示|返回|输出|给出|查看|列明"
ALL = re.compile(rf"逐(?P<each>{GROUP}|类|组)(?=\s*(?:{DISPLAY}))|"
                 rf"(?:{DISPLAY})\s*(?:全部|所有|每个)的?(?P<after>{GROUP})|"
                 rf"(?:全部|所有|每个)的?(?P<before>{GROUP})\s*(?:都|全部|分别)?\s*(?:{DISPLAY})|"
                 rf"(?:恢复|改为|改成|显示|展示|列出)\s*(?:全部|全量)(?=\s*(?:$|[。！？!?]))")
TOP = re.compile(rf"(?:(?:{DISPLAY}|改成|改为|取|只看|仅看)\s*(?:(?:按)?(?:差额|变化额|贡献)(?:最大的?)?\s*)?|"
                 rf"(?:^|\s)(?:只|仅)?\s*)前\s*(?P<number>\d+|[一二两三四五六七八九十百]+)"
                 rf"\s*(?:名(?P<rank_group>{GROUP})?|个(?P<count_group>{GROUP})|(?=$|[。！？!?]))")
NEGATION = re.compile(r"不要|不用|无需|不必|不需要|别(?:再|只|列出|展示|显示)|不(?:再)?(?:列出|展示|显示)|非要")
PARTITION = re.compile(r"(?:每(?:个)?|各)(?:品类|类别|商家|地区|省份|分组|月).*(?:前|冠军|第一)")
DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九")} | {"两": 2}


def _count(word):
    if word.isdecimal():
        return int(word) if len(word) <= 3 else 101
    if word in DIGITS:
        return DIGITS[word]
    if word == "一百":
        return 100
    match = re.fullmatch(r"([一二两三四五六七八九])?十([一二三四五六七八九])?", word)
    return 10 * DIGITS[match[1] or "一"] + DIGITS[match[2] or "零"] if match else None


def apply_result_selection(plan, question, trace):
    steps = plan.steps if plan.intent == "workflow" else [plan]
    candidates = [i for i, step in enumerate(steps)
                  if step.intent == "contribution" or (step.intent == "clarify" and step.pending_intent == "contribution")]
    if not candidates:
        return plan
    # In compound requests, the explicit selector must identify the right output.
    has_ranking = any(step.intent == "rank" for step in steps)
    selections = {}
    for clause in re.split(r"[，,、；;。？！?!\n]+", question):
        if NEGATION.search(clause) or PARTITION.search(clause):
            continue
        matches = [(m, "all", None, next((value for value in m.groups() if value), None)) for m in ALL.finditer(clause)]
        matches += [(m, "top_n", _count(m["number"]), m["rank_group"] or m["count_group"]) for m in TOP.finditer(clause)]
        for match, mode, top_n, group in matches:
            target = GROUPS.get(group)
            eligible = [i for i in candidates if target is None or target in dimensions(steps[i])]
            if len(eligible) != 1 or (group is None and has_ranking) or (mode == "top_n" and top_n is None):
                continue
            index = eligible[0]
            if index in selections and selections[index][1:3] != (mode, top_n):
                raise PlanError("本轮同时要求不同的结果范围，请明确显示全部分组还是只显示前 N 名。")
            if top_n is not None and not 1 <= top_n <= 100:
                raise PlanError("请将前 N 名设在 1 到 100 之间，或明确要求显示全部分组。")
            selections[index] = (match.group(), mode, top_n)
    if not selections:
        return plan
    steps = list(steps)
    for index, (phrase, mode, top_n) in selections.items():
        step = steps[index]
        updates = {"limit_mode": mode}
        if top_n is not None:
            updates["top_n"] = top_n
        steps[index] = step.model_copy(update=updates)
        trace.record("result_selection_applied", source="explicit_question", phrase=phrase,
                     step=index+1 if plan.intent == "workflow" else None,
                     before={key: getattr(step, key) for key in updates}, effective=updates)
    return plan.model_copy(update={"steps": steps}) if plan.intent == "workflow" else steps[0]
