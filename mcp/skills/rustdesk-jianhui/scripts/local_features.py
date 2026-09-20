"""Backward-compat shim: the implementations moved to _framework.vision."""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))  # mcp/skills, for _framework
from _framework.vision import BattleAnchors, green_label

__all__ = ['BattleAnchors', 'green_label']
