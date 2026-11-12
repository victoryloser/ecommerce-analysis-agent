import pytest

from app.baseline import calculate
from app.config import DATA_DIR, ROOT
from app.executor import Executor
from app.import_data import import_dataset
from app.source import SCHEMA


def prepare_local_demo(dataset_id):
    paths = ([ROOT / "material/ecommerce_agent_demo.xlsx"] if dataset_id == "excel_full" else
             [ROOT / "material/ecommerce_agent_demo_csv" / f"{table}.csv" for table in SCHEMA])
    if not all(path.is_file() for path in paths):
        pytest.skip(f"本地样例 {dataset_id} 未提供；仓库不分发数据文件")
    if not (DATA_DIR / f"{dataset_id}.manifest.json").exists():
        import_dataset(dataset_id)
    return dataset_id


@pytest.fixture(scope="session")
def local_csv_dataset():
    return prepare_local_demo("csv_small")


@pytest.fixture(scope="session", params=["excel_full", "csv_small"])
def dataset(request):
    return prepare_local_demo(request.param)


@pytest.fixture(scope="session")
def executor(dataset):
    return Executor(dataset)


@pytest.fixture(scope="session")
def expected(dataset):
    return calculate(dataset)
