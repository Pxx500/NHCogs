from importlib import import_module

__red_end_user_data_statement__ = (
    "This temporary migration helper stores migration run IDs, Discord guild "
    "and channel IDs used for moderator reports, the ordered Red package list, "
    "source commit, validation results, local artifact paths, checksums, and "
    "migration state. It also creates local backups of existing NHMisc and "
    "Honeypot data and Config values. Finalization does not delete those backups."
)


async def setup(bot) -> None:
    migrator = import_module(f"{__package__}.migrator")
    await bot.add_cog(migrator.NHCogsMigrator(bot))
