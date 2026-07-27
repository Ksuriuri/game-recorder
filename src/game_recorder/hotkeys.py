"""Recording toggle + idle-only radius adjust hotkeys."""

VK_CAPSLOCK = 0x14
# US keyboard OEM keys: [ ] \
VK_OEM_4 = 0xDB  # [
VK_OEM_6 = 0xDD  # ]
VK_OEM_5 = 0xDC  # \

HOTKEY_LABEL = "连按两次大写键"
HOTKEY_HINT = "连续按两次 Caps Lock（大写键）"
HOTKEY_SEQUENCE_LENGTH = 2
HOTKEY_DEBOUNCE_SECONDS = 0.5
HOTKEY_SEQUENCE_TIMEOUT_SECONDS = 1.0

# Radius adjust (only while not recording).
RADIUS_STEP_M = 1.0
RADIUS_MIN_M = 1.0
RADIUS_MAX_M = 20.0
RADIUS_HOTKEY_HINT = "[ ] 调半径  \\ 重置"

# Excluded from forbidden-key auto-stop while recording.
HOTKEY_VKS: frozenset[int] = frozenset((VK_CAPSLOCK,))
