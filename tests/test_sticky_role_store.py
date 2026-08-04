import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

MODULE_PATH = Path(__file__).parents[1] / "NHMisc" / "sticky_roles.py"
SPEC = importlib.util.spec_from_file_location("nhmisc_sticky_role_store_test", MODULE_PATH)
sticky_roles = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sticky_roles
SPEC.loader.exec_module(sticky_roles)


class StickyRoleStoreDataDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_user_everywhere_removes_roles_from_every_guild(self):
        with TemporaryDirectory() as directory:
            store = sticky_roles.StickyRoleStore(
                Path(directory) / "sticky_roles.sqlite3"
            )
            await store.initialize()
            await store.replace_member_roles(1, 42, {100, 200})
            await store.replace_member_roles(2, 42, {300})
            await store.replace_member_roles(1, 99, {400})

            await store.delete_user_everywhere(42)

            self.assertEqual(await store.get_member_roles(1, 42), set())
            self.assertEqual(await store.get_member_roles(2, 42), set())
            self.assertEqual(await store.get_member_roles(1, 99), {400})


if __name__ == "__main__":
    unittest.main()
