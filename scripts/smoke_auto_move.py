"""Manual / semi-auto smoke for auto-move across the four supported games.

Usage (game already running and focused)::

    uv run python scripts/smoke_auto_move.py --seconds 3

This only injects WASD + relative mouse look via SendInput. It does not start
a recording session. For full closed-loop recording::

    game-recorder --no-hotkey

Per-game checklist (action log should show key/mouse events):
  - GTA5 Story Mode
  - RDR2 Story Mode
  - Cyberpunk 2077
  - Black Myth Wukong
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_recorder.auto_move.input_inject import VK_W, InputInjector
from game_recorder.auto_move.policy_wander import WanderPolicy, apply_action


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test auto-move SendInput")
    parser.add_argument("--seconds", type=float, default=3.0, help="Inject duration")
    parser.add_argument("--hz", type=float, default=250.0, help="Mouse inject Hz")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("error: Windows only", file=sys.stderr)
        sys.exit(1)

    duration = max(0.1, float(args.seconds))
    hz = max(60.0, float(args.hz))
    interval = 1.0 / hz

    print(f"Focus the game window. Injecting W + look for {duration:g}s …")
    time.sleep(1.0)

    injector = InputInjector()
    policy = WanderPolicy(repath_min_s=2.0, repath_max_s=3.0)
    policy.reset()
    deadline = time.monotonic() + duration
    last = time.perf_counter()
    try:
        while time.monotonic() < deadline:
            now = time.perf_counter()
            dt = min(now - last, 4.0 / hz)
            last = now
            action = policy.step(None, dt=dt)
            apply_action(injector, action, dt=dt, pixels_per_deg=6.0)
            time.sleep(max(0.0, interval - (time.perf_counter() - now)))
    finally:
        injector.release_all()
        print(f"done (last keys would include W={VK_W:#x})")


if __name__ == "__main__":
    main()
