from redbot.core import commands

SNOWFLAKE_THRESHOLD = 2**63
MIN_SNOWFLAKE_DIGITS = 17


def parse_raw_message_id(argument: str) -> int:
    if (
        argument.isnumeric()
        and len(argument) >= MIN_SNOWFLAKE_DIGITS
        and int(argument) < SNOWFLAKE_THRESHOLD
    ):
        return int(argument)
    raise ValueError(f"{argument} does not look like a valid message ID")


class RawMessageId(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> int:
        try:
            return parse_raw_message_id(argument)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from error
