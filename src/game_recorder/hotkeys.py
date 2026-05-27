"""Recording toggle hotkey — double-tap Caps Lock."""

VK_CAPSLOCK = 0x14

HOTKEY_LABEL = "连按两次大写键"
HOTKEY_HINT = "连续按两次 Caps Lock（大写键）"
HOTKEY_SEQUENCE_LENGTH = 2
HOTKEY_DEBOUNCE_SECONDS = 0.5
HOTKEY_SEQUENCE_TIMEOUT_SECONDS = 1.0

# Excluded from forbidden-key auto-stop while recording.
HOTKEY_VKS: frozenset[int] = frozenset((VK_CAPSLOCK,))
