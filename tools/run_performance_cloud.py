"""Offline prepare or execute one exactly approved staged performance evaluation."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coppertop.performance_cloud import main

if __name__ == '__main__':
    raise SystemExit(main())
