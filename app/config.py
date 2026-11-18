from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]
INSTANCE_DIR = Path(os.environ.get("ANALYSIS_INSTANCE_DIR", ROOT)).resolve()
DATA_DIR = INSTANCE_DIR / "data"
RUNS_DIR = INSTANCE_DIR / "runs"
DATASETS = {
    "excel_full": "Excel 完整数据（2024—2025）",
    "csv_small": "CSV 小样本（2025 年上半年）",
}
METADATA_VERSION = "1.0.0"
QUERY_TIMEOUT = 5
MAX_ROWS = 500
