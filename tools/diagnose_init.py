#!/usr/bin/env python3
"""
Diagnose a component-initialization hang without Home Assistant.

Talks directly to an existing Modbus TCP target (proxy or inverter).
Replicates the exact sequence ProfileProvider.init(self.get) runs:
  1. Solarman object created
  2. auto-detection execute (Deye registers @ 0x0000)
  3. Bexie-specific registers

Each step has a hard timeout so a hang is immediately visible.
Run from the repo root:
  # against the HA proxy running on mass:
  python3 tools/diagnose_init.py --host 192.168.178.231 --port 1502
  # against the inverter directly (only if nothing else is connected):
  python3 tools/diagnose_init.py --host 192.168.178.134 --port 502
"""
import asyncio, importlib.util, sys, time, types, argparse
from functools import wraps
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# ---------------------------------------------------------------------------
# Minimal stubs so pysolarman loads without homeassistant installed
# ---------------------------------------------------------------------------

def _stub_pkg(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    # wire up parent
    parts = name.split('.')
    if len(parts) > 1:
        parent = sys.modules.get('.'.join(parts[:-1]))
        if parent:
            setattr(parent, parts[-1], mod)
    return mod

for _n in ['custom_components', 'custom_components.solarman']:
    sys.modules.setdefault(_n, _stub_pkg(_n))

# Provide the four helpers pysolarman imports from ..common
_common = _stub_pkg('custom_components.solarman.common')

def _retry(ignore=()):
    def decorator(f):
        @wraps(f)
        async def wrapper(*args, **kwargs):
            try:
                return await f(*args, **kwargs)
            except ignore:
                raise
            except Exception:
                return await f(*args, **kwargs)
        return wrapper
    return decorator

def _throttle(delay=1):
    def decorator(f):
        last = [0.0]
        @wraps(f)
        async def wrapper(*args, **kwargs):
            wait = delay - (time.monotonic() - last[0])
            if wait > 0:
                await asyncio.sleep(wait)
            last[0] = time.monotonic()
            return await f(*args, **kwargs)
        return wrapper
    return decorator

def _create_task(coro, *, name=None, context=None):
    return asyncio.get_running_loop().create_task(coro, name=name, context=context)

def _fmt(value):
    return value if not isinstance(value, (bytes, bytearray)) else value.hex(' ')

_common.retry = _retry
_common.throttle = _throttle
_common.create_task = _create_task
_common.format = _fmt

# Load pysolarman via importlib (bypasses package __init__.py)
_pys_dir = REPO / 'custom_components/solarman/pysolarman'
_spec = importlib.util.spec_from_file_location(
    'custom_components.solarman.pysolarman',
    _pys_dir / '__init__.py',
    submodule_search_locations=[str(_pys_dir)],
)
_pys = importlib.util.module_from_spec(_spec)
sys.modules['custom_components.solarman.pysolarman'] = _pys
_spec.loader.exec_module(_pys)

Solarman = _pys.Solarman

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STEP_TIMEOUT = 30  # seconds per step before declaring a hang

async def step(label, coro, timeout=STEP_TIMEOUT):
    print(f"\n▶  {label}", flush=True)
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
        print(f"   ✓  {time.monotonic()-t0:.2f}s  →  {result}", flush=True)
        return result
    except asyncio.TimeoutError:
        print(f"   ✗  HUNG — no response after {timeout}s", flush=True)
        return None
    except Exception as exc:
        print(f"   ✗  {time.monotonic()-t0:.2f}s  {type(exc).__name__}: {exc}", flush=True)
        return None

# ---------------------------------------------------------------------------
# Main diagnostic
# ---------------------------------------------------------------------------

async def run(host, port):
    TIMEOUT = 5  # same as TIMINGS_INTERVAL in HA

    print("=" * 60)
    print(f"Solarman → {host}:{port}  (modbus_tcp, timeout={TIMEOUT}s)")
    print(f"execute timeout = timeout*6 = {TIMEOUT*6}s")
    print("=" * 60)

    sm = Solarman(host, port, "modbus_tcp", 0, 1, TIMEOUT)
    print(f"\n▶  Solarman object created")

    # ── auto-detection (what profile=Auto does first) ───────────────────────
    await step(
        "execute 0x03 @ 0x0000 count=23  (auto-detection, Deye device type)",
        sm.execute(0x03, 0x0000, count=23),
    )

    # ── known Bexie registers ───────────────────────────────────────────────
    await step("execute 0x03 @ 0x2000 count=1  (Battery SOC)",
               sm.execute(0x03, 0x2000, count=1))

    await step("execute 0x03 @ 0x1010 count=2  (PV1 voltage + current)",
               sm.execute(0x03, 0x1010, count=2))

    await step("execute 0x03 @ 0x1300 count=2  (Grid power)",
               sm.execute(0x03, 0x1300, count=2))

    # ── Cleanup ─────────────────────────────────────────────────────────────
    print("\n▶  Closing Solarman")
    await sm.close()
    print("   done.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.178.231",
                    help="Host to connect to (default: HA server running the proxy)")
    ap.add_argument("--port", default=1502, type=int,
                    help="Port to connect to (default: 1502, the HA proxy port)")
    args = ap.parse_args()

    asyncio.run(run(args.host, args.port))


if __name__ == "__main__":
    main()
