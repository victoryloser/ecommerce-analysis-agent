"""Exact ratio components survive JSON, CSV, and session restoration."""
from decimal import Decimal, localcontext, ROUND_HALF_UP, InvalidOperation


def ratio_value(numerator, denominator):
    if numerator is None or denominator is None:
        return None, "insufficient_data"
    numerator, denominator = Decimal(str(numerator)), Decimal(str(denominator))
    if denominator == 0:
        return None, "undefined"
    with localcontext() as ctx:
        ctx.prec = 80
        return numerator / denominator, "determined"


def resolve_ratios(columns, rows, specs):
    metadata = []
    for row in rows:
        values = dict(zip(columns, row))
        entry = {}
        for column, (numerator, denominator) in specs.items():
            if not {column, numerator, denominator} <= values.keys():
                continue
            n, d = values[numerator], values[denominator]
            value, status = ratio_value(n, d)
            row[columns.index(column)] = value
            entry[column] = {"numerator": n, "denominator": d, "calculation_status": status}
        metadata.append(entry)
    return metadata if specs else []


def restore_numbers(columns, rows, ratios):
    monetary = {"sales", "seller_sales", "freight", "payment_amount", "price", "freight_value", "payment_value",
                "order_sales", "order_sales_known", "order_freight", "order_freight_known", "payment_known",
                "total_change", "parent_change", "previous_sales", "current_value", "previous_value", "absolute_change"}
    for index, row in enumerate(rows):
        for col, value in zip(columns, row):
            base = col.removesuffix("_current").removesuffix("_previous").removesuffix("_change")
            if isinstance(value, str) and (col in monetary or base in monetary):
                try:
                    row[columns.index(col)] = Decimal(value)
                except InvalidOperation:
                    # Custom SQL can deliberately give a text column this name.
                    pass
        for col, parts in (ratios[index] if index < len(ratios) else {}).items():
            for key in ("numerator", "denominator"):
                if parts[key] is not None:
                    parts[key] = Decimal(str(parts[key]))
            if col in columns:
                row[columns.index(col)] = ratio_value(parts["numerator"], parts["denominator"])[0]


def display_decimal(value, multiplier=1):
    with localcontext() as ctx:
        ctx.prec = 90
        return str((Decimal(str(value)) * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
