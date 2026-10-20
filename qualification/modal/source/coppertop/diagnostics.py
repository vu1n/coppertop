"""Bounded failure locations; exception values, paths, locals and secrets stay out."""
from pathlib import Path
import re
import traceback

SAFE_MESSAGES = frozenset({
    'Reset parity failed', 'Branch/query-start prefix differs',
    'Baseline ended before required 16-control tape',
    'Actual decoded candidate repeat/diversity gate failed',
    'Pinned simulator 20Hz control clock required',
    'Control/audit duration exceeds real-time period',
    'Simulator pacing overrun invalidates query delay',
    'No query handoff before pending queue/comparison ended',
    'Owned native server startup failed',
    'Official14D state view differs', 'Official query history differs from recorded frames',
    'Official camera history differs from recorded frames',
    'Executed action differs from official cadence/chunk',
    'Pinned official20Hz control clock required',
    'Owned frozen official server readiness differs',
})


def failure(error, component):
    """Only our enumerated guard messages are public; unknown text is omitted."""
    message = str(error)
    owned = Path(__file__).resolve().parent
    frames = traceback.extract_tb(error.__traceback__)[-12:]
    local = any(Path(f.filename).parent == owned for f in frames)
    safe = local and (message in SAFE_MESSAGES or re.fullmatch(r'Replay parity failed at control [0-9]{1,3}', message))
    clean = lambda text: re.sub(r'[^A-Za-z0-9_.<>-]', '_', text)[:80]
    return {'error_type': clean(type(error).__name__), 'component': clean(component),
        'message': message[:160] if safe else 'exception_text_omitted',
        'traceback': [{'file': clean(Path(f.filename).name), 'line': f.lineno,
                       'function': clean(f.name)} for f in frames]}
