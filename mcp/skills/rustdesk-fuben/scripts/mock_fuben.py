#!/usr/bin/env python3
"""Dungeon mock adds cursor travel without changing the shared mock."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _framework import mock


@mock.server.tool()
async def mouse_move(device: str = 'pc1', x: int = 0, y: int = 0) -> str:
    mock.record(mock.window_key(device, x), 'mouse_move', x, y)
    return f'mock cursor moved to {x},{y}'


if __name__ == '__main__':
    mock.main()
