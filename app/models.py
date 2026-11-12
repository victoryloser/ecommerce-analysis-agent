from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DatasetId = str
MetricId = Literal["sales", "order_count", "delay_rate", "avg_review", "low_score_rate", "repeat_customers", "payment_amount"]
Dimension = Literal["total", "month", "category", "seller", "delivery_status", "customer_state"]
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


class AnalysisPlan(Contract):
    dataset_id: DatasetId
    metadata_version: str
    intent: Literal["aggregate", "rank", "compare", "explain", "clarify", "unsupported", "load_catalog", "join_example", "contribution", "details", "diagnose", "workflow", "history"]
    metric_ids: list[MetricId] = Field(default_factory=list)
    dimension: Dimension = "total"
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
    diagnosis: Literal["reconciliation", "quality", "sales_spike", "seller_outlier"] = "reconciliation"
    diagnostic_threshold: float | None = Field(default=None, ge=0)
    steps: list["AnalysisPlan"] = Field(default_factory=list, max_length=4)
    source_step: int | None = Field(default=None, ge=1, le=4)
    source_row: int = Field(default=0, ge=0)
    bind_field: Literal["category_id", "seller_id", "customer_state", "order_id"] | None = None
    history_query: str | None = None
    history_start: date | None = None
    history_end: date | None = None


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
