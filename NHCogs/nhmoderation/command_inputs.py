from dataclasses import dataclass

DEFAULT_DAYS = 30
DEFAULT_AMOUNT = 10
MAX_AMOUNT = 20
MAX_POSITIONAL_ARGUMENTS = 2


@dataclass(frozen=True)
class BanChartArguments:
    days: int | None
    amount: int
    include_automation: bool


def parse_banchart_arguments(arguments: str) -> BanChartArguments:
    tokens = arguments.split()
    include_automation = False
    positional: list[str] = []
    for token in tokens:
        if token == "--automation":
            if include_automation:
                raise ValueError("Option --automation may be used only once")
            include_automation = True
        elif token.startswith("--"):
            raise ValueError(f"Unknown option: {token}")
        else:
            positional.append(token)
    if len(positional) > MAX_POSITIONAL_ARGUMENTS:
        raise ValueError("Expected at most days and amount")
    days = _parse_days(positional[0]) if positional else DEFAULT_DAYS
    amount = _parse_amount(positional[1]) if len(positional) == MAX_POSITIONAL_ARGUMENTS else DEFAULT_AMOUNT
    return BanChartArguments(days, amount, include_automation)


def _parse_days(value: str) -> int | None:
    if value.casefold() == "all":
        return None
    try:
        days = int(value)
    except ValueError as error:
        raise ValueError("Days must be a positive number or all") from error
    if days < 1:
        raise ValueError("Days must be at least 1")
    return days


def _parse_amount(value: str) -> int:
    try:
        amount = int(value)
    except ValueError as error:
        raise ValueError("Amount must be a number between 1 and 20") from error
    if not 1 <= amount <= MAX_AMOUNT:
        raise ValueError("Amount must be between 1 and 20")
    return amount
