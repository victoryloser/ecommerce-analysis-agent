from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
DATASETS = {
    "excel_full": "Excel 完整数据（2024—2025）",
    "csv_small": "CSV 小样本（2025 年上半年）",
}
METADATA_VERSION = "1.0.0"
QUERY_TIMEOUT = 5
MAX_ROWS = 500
