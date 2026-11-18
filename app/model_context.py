"""Choose local entity matches and limit optional business data sent to a model."""
from contextvars import ContextVar
import re

OUTBOUND = ContextVar("outbound_fields", default=None)


def permitted(field):
    allowed = OUTBOUND.get()
    return allowed is None or field in allowed


def entity_candidates(manifest, question):
    text = question.casefold()
    matches = {}
    aliases = manifest.get("entity_aliases", {})
    for group in ("categories", "sellers"):
        candidates = []
        for key, label in manifest["entities"][group]:
            terms = [str(key), str(label), *aliases.get(group, {}).get(key, [])]
            score = max((len(term) for term in terms if mentioned(term, text)), default=0)
            if score:
                candidates.append((score, key, label))
        candidates.sort(key=lambda r: (-r[0], r[1]))
        matches[group] = [[key, label] for _, key, label in candidates[:16]]
    return matches


def mentioned(term, text):
    term = term.casefold()
    if not term or term not in text:
        return False
    if term.isascii() and (term[0].isalnum() or term[-1].isalnum()):
        return re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", text) is not None
    return True


def explicit_entities(manifest, question):
    selected = []
    quoted = r"""(商家|品类)\s*(?:ID|名称|名)?\s*(?:为|是|=|：|:)?\s*[“「"]([^”」"]+)[”」"]"""
    ids = r"(商家|品类)\s*ID\s*(?:为|是|=|：|:)\s*([a-zA-Z0-9_-]+)"
    for kind, name in [*re.findall(quoted, question), *re.findall(ids, question)]:
        group = "sellers" if kind == "商家" else "categories"
        aliases = manifest.get("entity_aliases", {}).get(group, {})
        keys = {key for key, label in manifest["entities"][group]
                if name.casefold() in {str(v).casefold() for v in (key, label, *aliases.get(key, []))}}
        if not keys:
            raise ValueError(f"{kind}“{name}”没有匹配记录，请核对名称或 ID")
        selected.append(("seller_id" if group == "sellers" else "category_id", keys))
    return selected


def question_context(question, manifest):
    if not permitted("question"):
        raise ValueError("此实例的外发设置未允许发送当前问题")
    return entity_candidates(manifest, question) if permitted("entities") else {"categories": [], "sellers": []}


def session_context(session):
    summary = session.summary() if permitted("scope") else {}
    allowed = OUTBOUND.get()
    if allowed is not None:
        summary.pop("last_result", None)
        result = session.last_result
        if result:
            indices = [i for i, name in enumerate(result.columns) if f"preview:{name}" in allowed]
            if indices:
                summary["last_result"] = {"columns": [result.columns[i] for i in indices],
                                          "preview": [[r[i] for i in indices] for r in result.rows[:3]]}
    summary["recent_messages"] = [m for m in session.history if m["role"] == "user"][-4:] if permitted("history") else []
    return summary
