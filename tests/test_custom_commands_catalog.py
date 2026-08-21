import importlib.util
import inspect
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs" / "custom_commands"


def load_catalog_modules():
    package_name = "custom_commands_catalog_subject"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PACKAGE_PATH)]
    sys.modules[package_name] = package
    discord = types.ModuleType("discord")
    commands = types.ModuleType("redbot.core.commands")
    commands.Parameter = inspect.Parameter
    core = types.ModuleType("redbot.core")
    core.commands = commands
    temporary_modules = {
        "discord": discord,
        "redbot": types.ModuleType("redbot"),
        "redbot.core": core,
        "redbot.core.commands": commands,
    }
    previous = {name: sys.modules.get(name) for name in temporary_modules}
    sys.modules.update(temporary_modules)
    try:
        for module_name in ("migration_state", "arguments", "catalog"):
            qualified_name = f"{package_name}.{module_name}"
            spec = importlib.util.spec_from_file_location(
                qualified_name,
                PACKAGE_PATH / f"{module_name}.py",
            )
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[qualified_name] = module
            spec.loader.exec_module(module)
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module
    return (
        sys.modules[f"{package_name}.catalog"],
        sys.modules[f"{package_name}.migration_state"],
    )


catalog, migration_state = load_catalog_modules()


class CustomCommandCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_normalizes_name_and_assigns_default_weight(self):
        created_at = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            store = catalog.CustomCommandCatalog(Path(directory) / "commands.sqlite")
            await store.initialize()

            command = await store.create(
                guild_id=100,
                name="  Hello  ",
                author_id=200,
                author_name="Moderator",
                responses=(catalog.ResponseDraft("Hello {author.name}"),),
                created_at=created_at,
            )

        self.assertIsNotNone(command)
        self.assertEqual(command.name, "hello")
        self.assertEqual(command.revision, 1)
        self.assertEqual(command.created_at, created_at)
        self.assertEqual(len(command.responses), 1)
        self.assertEqual(command.responses[0].display_order, 0)
        self.assertEqual(command.responses[0].weight, 100)
        self.assertTrue(command.responses[0].response_id)

    async def test_edit_is_revision_checked_and_preserves_response_identity(self):
        with TemporaryDirectory() as directory:
            store = catalog.CustomCommandCatalog(Path(directory) / "commands.sqlite")
            await store.initialize()
            created = await store.create(
                guild_id=100,
                name="hello",
                author_id=200,
                author_name="Creator",
                responses=(catalog.ResponseDraft("first"),),
            )
            response_id = created.responses[0].response_id
            edited = await store.edit(
                guild_id=100,
                name="hello",
                expected_revision=1,
                editor_id=300,
                editor_name="Editor",
                responses=(
                    catalog.ResponseDraft(
                        "updated",
                        weight=250,
                        response_id=response_id,
                    ),
                ),
            )

            with self.assertRaises(catalog.StaleRevision):
                await store.edit(
                    guild_id=100,
                    name="hello",
                    expected_revision=1,
                    editor_id=400,
                    editor_name="Stale editor",
                    responses=(catalog.ResponseDraft("must not commit"),),
                )
            stored = await store.get(100, "hello")

        self.assertEqual(edited.revision, 2)
        self.assertEqual(edited.responses[0].response_id, response_id)
        self.assertEqual(edited.responses[0].weight, 250)
        self.assertEqual(edited.editors[0].user_id, 300)
        self.assertEqual(stored, edited)

    async def test_invalid_random_signature_never_creates_partial_command(self):
        with TemporaryDirectory() as directory:
            store = catalog.CustomCommandCatalog(Path(directory) / "commands.sqlite")
            await store.initialize()

            with self.assertRaisesRegex(
                catalog.InvalidCommand,
                "same arguments",
            ):
                await store.create(
                    guild_id=100,
                    name="broken",
                    author_id=200,
                    author_name="Creator",
                    responses=(
                        catalog.ResponseDraft("Hello {1}"),
                        catalog.ResponseDraft("Hello {1} {2}"),
                    ),
                )

            self.assertEqual(await store.list_commands(100), ())

    async def test_import_rolls_back_every_command_when_one_conflicts(self):
        now = datetime.now(timezone.utc)
        first = catalog.CustomCommand(
            guild_id=100,
            name="first",
            author_id=200,
            author_name="Creator",
            created_at=now,
            edited_at=None,
            revision=1,
            responses=(catalog.CustomResponse("response-1", 0, "first", 100),),
            cooldowns={},
            editors=(),
        )
        duplicate = catalog.CustomCommand(
            guild_id=100,
            name="first",
            author_id=300,
            author_name="Other",
            created_at=now,
            edited_at=None,
            revision=1,
            responses=(catalog.CustomResponse("response-2", 0, "second", 100),),
            cooldowns={},
            editors=(),
        )
        with TemporaryDirectory() as directory:
            store = catalog.CustomCommandCatalog(Path(directory) / "commands.sqlite")
            await store.initialize()

            with self.assertRaises(catalog.InvalidCommand):
                await store.import_all((first, duplicate))

            self.assertEqual(await store.list_commands(100), ())

    async def test_migration_import_commits_commands_and_state_together(self):
        now = datetime.now(timezone.utc)
        command = catalog.CustomCommand(
            guild_id=100,
            name="first",
            author_id=200,
            author_name="Creator",
            created_at=now,
            edited_at=None,
            revision=1,
            responses=(catalog.CustomResponse("response-1", 0, "first", 100),),
            cooldowns={},
            editors=(),
        )
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "commands.sqlite"
            store = catalog.CustomCommandCatalog(database_path)
            state_store = migration_state.MigrationStateStore(database_path)
            await store.initialize()
            await state_store.initialize()
            await state_store.save(
                migration_state.MigrationPhase.PLANNED,
                source_digest="source-digest",
                destination_digest=None,
            )

            await store.import_migration(
                (command,),
                source_digest="source-digest",
                destination_digest="destination-digest",
            )

            stored = await store.list_commands(100)
            state = await state_store.get()

        self.assertEqual(stored, (command,))
        self.assertEqual(
            state.phase,
            migration_state.MigrationPhase.IMPORTED_NOT_ACTIVE,
        )
        self.assertEqual(state.source_digest, "source-digest")
        self.assertEqual(state.destination_digest, "destination-digest")

    async def test_migration_import_rolls_back_when_plan_state_changed(self):
        now = datetime.now(timezone.utc)
        command = catalog.CustomCommand(
            guild_id=100,
            name="first",
            author_id=200,
            author_name="Creator",
            created_at=now,
            edited_at=None,
            revision=1,
            responses=(catalog.CustomResponse("response-1", 0, "first", 100),),
            cooldowns={},
            editors=(),
        )
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "commands.sqlite"
            store = catalog.CustomCommandCatalog(database_path)
            state_store = migration_state.MigrationStateStore(database_path)
            await store.initialize()
            await state_store.initialize()
            await state_store.save(
                migration_state.MigrationPhase.PLANNED,
                source_digest="original-digest",
                destination_digest=None,
            )

            with self.assertRaisesRegex(RuntimeError, "plan state changed"):
                await store.import_migration(
                    (command,),
                    source_digest="stale-digest",
                    destination_digest="destination-digest",
                )

            stored = await store.list_commands(100)
            state = await state_store.get()

        self.assertEqual(stored, ())
        self.assertEqual(state.phase, migration_state.MigrationPhase.PLANNED)
        self.assertEqual(state.source_digest, "original-digest")
        self.assertIsNone(state.destination_digest)

    async def test_migration_import_rejects_nonempty_destination_atomically(self):
        now = datetime.now(timezone.utc)
        imported = catalog.CustomCommand(
            guild_id=100,
            name="imported",
            author_id=200,
            author_name="Creator",
            created_at=now,
            edited_at=None,
            revision=1,
            responses=(catalog.CustomResponse("response-1", 0, "imported", 100),),
            cooldowns={},
            editors=(),
        )
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "commands.sqlite"
            store = catalog.CustomCommandCatalog(database_path)
            state_store = migration_state.MigrationStateStore(database_path)
            await store.initialize()
            await state_store.initialize()
            await store.create(
                guild_id=100,
                name="existing",
                author_id=300,
                author_name="Existing creator",
                responses=(catalog.ResponseDraft("existing"),),
            )
            await state_store.save(
                migration_state.MigrationPhase.PLANNED,
                source_digest="source-digest",
                destination_digest=None,
            )

            with self.assertRaisesRegex(RuntimeError, "destination is not empty"):
                await store.import_migration(
                    (imported,),
                    source_digest="source-digest",
                    destination_digest="destination-digest",
                )

            commands = await store.list_commands(100)
            state = await state_store.get()

        self.assertEqual(tuple(command.name for command in commands), ("existing",))
        self.assertEqual(state.phase, migration_state.MigrationPhase.PLANNED)

    async def test_redaction_removes_author_and_editor_identity(self):
        with TemporaryDirectory() as directory:
            store = catalog.CustomCommandCatalog(Path(directory) / "commands.sqlite")
            await store.initialize()
            created = await store.create(
                guild_id=100,
                name="hello",
                author_id=200,
                author_name="Creator",
                responses=(catalog.ResponseDraft("hello"),),
            )
            await store.edit(
                guild_id=100,
                name="hello",
                expected_revision=created.revision,
                editor_id=200,
                editor_name="Creator",
            )

            changed = await store.redact_user(200)
            stored = await store.get(100, "hello")

        self.assertEqual(changed, 2)
        self.assertEqual(stored.author_id, catalog.DELETED_USER_ID)
        self.assertEqual(stored.author_name, catalog.DELETED_USER_NAME)
        self.assertEqual(stored.editors[0].user_id, catalog.DELETED_USER_ID)
        self.assertEqual(stored.editors[0].display_name, catalog.DELETED_USER_NAME)

    async def test_multiple_deleted_editors_merge_into_one_redacted_identity(self):
        with TemporaryDirectory() as directory:
            store = catalog.CustomCommandCatalog(Path(directory) / "commands.sqlite")
            await store.initialize()
            command = await store.create(
                guild_id=100,
                name="hello",
                author_id=100,
                author_name="Creator",
                responses=(catalog.ResponseDraft("hello"),),
            )
            command = await store.edit(
                guild_id=100,
                name="hello",
                expected_revision=command.revision,
                editor_id=200,
                editor_name="First editor",
            )
            await store.edit(
                guild_id=100,
                name="hello",
                expected_revision=command.revision,
                editor_id=300,
                editor_name="Second editor",
            )

            await store.redact_user(200)
            await store.redact_user(300)
            stored = await store.get(100, "hello")

        self.assertEqual(len(stored.editors), 1)
        self.assertEqual(stored.editors[0].user_id, catalog.DELETED_USER_ID)


if __name__ == "__main__":
    unittest.main()
