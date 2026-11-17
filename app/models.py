from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DatasetId = str
MetricId = Literal["sales", "freight", "order_count", "delay_rate", "avg_review", "low_score_rate", "repeat_customers", "payment_amount"]
Dimension = Literal["total", "month", "category", "seller", "delivery_status", "customer_state"]
GroupDimension = Literal["month", "category", "seller", "delivery_status", "customer_state"]
AnalysisIntent = Literal["aggregate", "rank", "compare", "contribution", "details", "diagnose", "join_example"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeRange(Contract):
    start: date
    end: date

    @model_validator(mode="after")
    def chronological(self):
        if self.start >= self.end:
            raise ValueError("时间范围必须左闭右开，start < end")
        return self


class EntityFilter(Contract):
    field: Literal["category_id", "seller_id", "customer_state"]
    values: list[str] = Field(min_length=1)


class EntityCandidate(Contract):
    field: Literal["category_id", "seller_id", "customer_state"]
    value: str
    label: str


class Predicate(Contract):
    field: Literal["order_id", "customer_state", "order_status", "delivery_status", "order_sales", "order_freight", "payment_amount", "review_score", "category_id", "seller_id", "product_id", "price", "freight_value"] | None = None
    operator: Literal["eq", "ne", "gt", "ge", "lt", "le", "in", "not_in", "between", "is_null", "not_null"] = "eq"
    values: list[str | int | float] = Field(default_factory=list)
    conjunction: Literal["all", "any"] = "all"
    children: list["Predicate"] = Field(default_factory=list)
    negate: bool = False


class PlanUpdate(Contract):
    operation: Literal["set", "remove", "reset"]
    field: Literal["category_id", "seller_id", "customer_state", "product_id", "price", "freight_value", "order_sales", "order_freight", "payment_amount", "review_score", "order_id", "time_range", "comparison_range", "dimension", "group_by", "source_partition", "rank_within", "contribution_within", "contribution_direction", "metric_ids", "order_statuses", "time_field", "order_where", "item_where", "intent", "top_n", "min_group_count", "detail_level", "limit_mode"] | None = None
    values: list[str | int | float] = Field(default_factory=list)
    predicate: Predicate | None = None
    period: TimeRange | None = None
    period_mode: Literal["explicit_range", "full_coverage"] | None = None
    comparison_mode: Literal["none", "fixed", "previous_month"] | None = None
    target_step: int | None = Field(default=None, ge=1, le=6)


class AnalysisPlan(Contract):
    dataset_id: DatasetId
    metadata_version: str
    intent: Literal["aggregate", "rank", "compare", "explain", "clarify", "unsupported", "load_catalog", "join_example", "contribution", "details", "diagnose", "workflow", "history", "update", "continue"]
    update: PlanUpdate | None = None
    updates: list[PlanUpdate] = Field(default_factory=list, max_length=16)
    scope_source: Literal["working", "analysis", "view"] = "working"
    output_mode: Literal["analysis", "seller_audit", "customer_summary", "quality_check"] = "analysis"
    limitations: list[Literal["causality", "profit"]] = Field(default_factory=list, max_length=2)
    limit_mode: Literal["top_n", "all"] = "top_n"
    binding_updates: list[PlanUpdate] = Field(default_factory=list, max_length=16)
    metric_ids: list[MetricId] = Field(default_factory=list)
    dimension: Dimension = "total"
    group_by: list[GroupDimension] | None = Field(default=None, max_length=2)
    rank_within: GroupDimension | None = None
    contribution_within: GroupDimension | None = None
    contribution_direction: Literal["increase", "decrease"] = "increase"
    time_range: TimeRange | None = None
    comparison_range: TimeRange | None = None
    filters: list[EntityFilter] = Field(default_factory=list)
    min_group_count: int = Field(default=1, ge=1)
    top_n: int = Field(default=10, ge=1, le=100)
    explanation_topic: Literal["joins", "metrics", "causality"] | None = None
    clarification: str | None = None
    missing_slots: list[Literal["year", "category_id", "seller_id", "customer_state", "metric_ids", "time_range"]] = Field(default_factory=list)
    pending_intent: AnalysisIntent | Literal["workflow"] | None = None
    month: int | None = Field(default=None, ge=1, le=12)
    comparison_month: int | None = Field(default=None, ge=1, le=12)
    entity_candidates: list[EntityCandidate] = Field(default_factory=list)
    ambiguous_value: str | None = None
    time_field: Literal["purchase_at", "approved_at", "shipped_at", "delivered_at", "estimated_delivery_at"] = "purchase_at"
    order_statuses: list[str] = Field(default_factory=lambda: ["delivered"], min_length=1)
    order_where: Predicate | None = None
    item_where: Predicate | None = None
    detail_level: Literal["orders", "items", "payments", "reviews"] = "orders"
    selected_order_id: str | None = None
    diagnosis: Literal["reconciliation", "quality", "sales_spike", "seller_outlier", "quality_summary", "quality_rows", "order_candidate", "order_audit"] = "reconciliation"
    quality_scope: Literal["current", "previous", "comparison_union"] = "comparison_union"
    quality_types: list[Literal["exact_duplicate", "price_conflict", "missing_payment", "other"]] = Field(default_factory=lambda: ["exact_duplicate", "price_conflict", "missing_payment"], min_length=1, max_length=4)
    audit_seller_id: str | None = None
    diagnostic_threshold: float | None = Field(default=None, ge=0)
    period_mode: Literal["explicit_range", "full_coverage"] = "explicit_range"
    comparison_mode: Literal["none", "fixed", "previous_month"] = "none"
    steps: list["AnalysisPlan"] = Field(default_factory=list, max_length=6)
    source_step: int | None = Field(default=None, ge=1, le=6)
    source_row: int = Field(default=0, ge=0)
    source_partition: str | None = None
    bind_field: Literal["category_id", "seller_id", "customer_state", "order_id"] | None = None
    bind_mode: Literal["field", "group"] = "field"
    history_query: str | None = None
    history_start: date | None = None
    history_end: date | None = None

    @model_validator(mode="after")
    def normalize_grouping(self):
        if self.update is not None and self.updates:
            raise ValueError("update 与 updates 不能同时提供")
        if self.group_by is not None:
            if len(set(self.group_by)) != len(self.group_by):
                raise ValueError("分组维度不能重复")
            if self.dimension != "total" and self.group_by != [self.dimension]:
                raise ValueError("group_by 与旧 dimension 冲突")
            if len(self.group_by) < 2:
                self.dimension = self.group_by[0] if self.group_by else "total"
                self.group_by = None
            else:
                self.group_by = sorted(self.group_by, key=["month", "category", "seller", "delivery_status", "customer_state"].index)
        return self


class QueryResult(Contract):
    query_id: str
    status: Literal["ok", "empty", "out_of_coverage", "timeout", "error"]
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    truncated: bool = False
    elapsed_ms: int = 0
    error: str | None = None
    row_offset: int = 0
    export_file: str | None = None
    row_count: int | None = None
    quality: list[dict] = Field(default_factory=list)
    ratios: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def restore_exact_values(self):
        from app.result_math import restore_numbers
        from decimal import Decimal
        restore_numbers(self.columns, self.rows, self.ratios)
        for row in self.quality:
            for q in row.values():
                if q and isinstance(q.get("known_value"), str):
                    q["known_value"] = Decimal(q["known_value"])
        return self


class ComparisonRequest(Contract):
    target_dataset_id: str
    mode: Literal["fixed_scope", "intent_replay"] = "fixed_scope"
    source: Literal["current_result", "working_plan"] = "current_result"
    source_record_id: str | None = None
