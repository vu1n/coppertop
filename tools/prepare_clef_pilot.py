"""Emit offline preparation only; no execute option or provider imports."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coppertop.evaluation_readiness import prepare

if __name__ == '__main__':
    if len(sys.argv) != 1:
        raise SystemExit('Usage: python tools/prepare_clef_pilot.py (offline only)')
    print(json.dumps(prepare(), indent=2, allow_nan=False))
