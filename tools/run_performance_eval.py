"""Prepare offline, or run explicitly inside an approved disposable CUDA job."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coppertop.performance_runtime import main

if __name__ == '__main__':
    main()
