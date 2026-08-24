"""重複通知を防ぐ状態ファイルの読み書き。"""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path

EMPTY_STATE = {
    "version": 1,
    "armed": False,
    "cases": {},
    "source_health": {},
}


def load_state(path: Path) -> dict:
    """状態を読み込む。壊れたJSONを無言で初期化しない。

    無言で初期化すると、既存案件が全部「新着」に見えて通知爆撃になるため、
    JSON破損時は明示的に停止する。
    """

    if not path.exists():
        return deepcopy(EMPTY_STATE)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"状態ファイルを読み込めません: {path}: {exc}") from exc

    if data.get("version") != 1 or not isinstance(data.get("cases"), dict):
        raise RuntimeError(f"状態ファイルの形式が不正です: {path}")

    data.setdefault("armed", False)
    data.setdefault("source_health", {})
    return data


def save_state(path: Path, state: dict) -> None:
    """途中終了でJSONが半壊しないよう、一時ファイルから原子的に置き換える。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

