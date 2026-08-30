import re

MESSAGE_LINK_PATTERN = re.compile(
    r"^https://(?:canary\.|ptb\.)?discord\.com/channels/(\d+)/(\d+)/(\d+)$"
)
