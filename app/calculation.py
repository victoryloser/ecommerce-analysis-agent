"""Host-bound monetary policy; neither the model nor a dataset label chooses it."""
from dataclasses import dataclass

LEGACY_RULE = "p0-amount-v1"
ROUND2_RULE = "round2-order-exclusion-v1"


@dataclass(frozen=True)
class CalculationContext:
    rule_version: str = LEGACY_RULE
    capabilities: dict | None = None

    def __post_init__(self):
        if self.rule_version not in {LEGACY_RULE, ROUND2_RULE}:
            raise ValueError(f"不支持的金额规则：{self.rule_version}；请使用原版本或创建迁移快照")

    @property
    def excludes_orders(self):
        return self.rule_version == ROUND2_RULE

    @classmethod
    def from_manifest(cls, manifest):
        # Pre-P0 manifests used the original full-amount semantics.
        return cls(manifest.get("quality_rule_version") or LEGACY_RULE, manifest.get("capabilities"))


LEGACY_CONTEXT = CalculationContext()


def exclusion_cte(context):
    if not context.excludes_orders:
        return None
    return f"amount_exclusions AS (SELECT DISTINCT order_id FROM quality_issues WHERE {price_conflict_sql()})"


def price_conflict_sql(alias=""):
    # Candidates are typed rows in SCHEMA order. NULL is not a conflicting price.
    from app.source import SCHEMA
    position = list(SCHEMA["order_items"]).index("price")
    a = alias + "." if alias else ""
    return f"""{a}table_name='order_items' AND {a}field_name='price' AND {a}reason='amount_conflict'
        AND list_count(list_distinct(TRY_CAST(json_extract({a}candidates,'$[*][{position}]')
            AS DECIMAL(18,2)[])))>=2"""


def eligible_sql(context, alias="o"):
    return f"NOT EXISTS (SELECT 1 FROM amount_exclusions e WHERE e.order_id={alias}.order_id)" if context.excludes_orders else "TRUE"
