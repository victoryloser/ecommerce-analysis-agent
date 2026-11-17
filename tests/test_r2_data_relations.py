"""Optional formal-pack format and business relations, separate from the 32 turns."""
from openpyxl import load_workbook
import pytest

from app.calculation import ROUND2_RULE
from app.import_data import import_tables
from app.oracle import SourceOracle, check_rows
from app.round2_suite import contract
from app.source import SCHEMA
from test_round2_reference import PACKAGE, public_data, pytestmark
from test_round2_amounts import direct


@pytest.mark.parametrize("role", ["D1", "D2", "D3", "D4"])
def test_csv_excel_business_equivalence(public_data, tmp_path, role):
    csv_dir, datasets = public_data
    csv_manifest, _, csv_oracle = datasets[role]
    path = PACKAGE/"数据集"/role/(role+".xlsx")
    book = load_workbook(path, read_only=True, data_only=True)
    tables = {}
    for name in SCHEMA:
        values = book[name].iter_rows(values_only=True)
        headers = next(values)
        tables[name] = [dict(zip(headers, row)) for row in values]
    book.close()
    manifest = import_tables(role+"-excel", tables, [path], role, tmp_path, quality_rule_version=ROUND2_RULE)
    assert manifest["semantic_fingerprints"] == csv_manifest["semantic_fingerprints"]
    excel_oracle = SourceOracle(manifest["dataset_id"], data_dir=tmp_path)
    for case in ("A1", "A2", "A3", "A4", "B2", "B3", "B4"):
        source = contract(csv_manifest, case)
        target = contract(manifest, case)
        assert csv_oracle.evaluate(source, with_quality=True) == excel_oracle.evaluate(target, with_quality=True)
        if case != "B2":
            metrics = [[m] for m in target.metric_ids] if case == "B4" else [target.metric_ids]
            for selected in metrics:
                query = target.model_copy(update={"metric_ids": selected})
                _, result = direct(tmp_path, manifest, **query.model_dump(exclude={"dataset_id", "metadata_version"}))
                check_rows(result, csv_oracle.evaluate(source.model_copy(update={"metric_ids": selected}), with_quality=True))


def test_d1_d2_equivalent_business_not_physical_payments(public_data):
    _, datasets = public_data
    m1, _, o1 = datasets["D1"]
    m2, _, o2 = datasets["D2"]
    assert m1["row_counts"]["order_payments"] != m2["row_counts"]["order_payments"]
    assert o1.payments == o2.payments
    for case in ("A1", "A2", "A3", "A4", "B2", "B3", "B4"):
        assert o1.evaluate(contract(m1, case)) == o2.evaluate(contract(m2, case))
