import re
import pandas as pd
from src.errors import LimitExceeded
from src.processing.records import canonical


def csv_bytes(
    frame: pd.DataFrame, labels: tuple[tuple[str, str], ...], max_bytes: int
) -> bytes:
    def cell(value):
        if isinstance(value, (list, dict)):
            return canonical(value)
        if isinstance(value, str) and (
            value.lstrip().startswith(("=", "+", "-", "@"))
            or value.startswith(("\t", "\r", "\n"))
        ):
            return "'" + value
        return value

    safe = frame.copy()
    for column in safe:
        safe[column] = safe[column].map(cell)
    safe.columns = [label for _, label in labels]
    content = safe.to_csv(index=False).encode("utf-8-sig")
    if len(content) > max_bytes:
        raise LimitExceeded("单 CSV 超过大小上限，本次数据集未发布。")
    return content


def filename(root: str, dataset: str, stamp: str) -> str:
    safe = re.sub(r"[^\w.-]", "_", root, flags=re.UNICODE)[:80]
    return f"trace_{safe}_{dataset}_{stamp}.csv"
