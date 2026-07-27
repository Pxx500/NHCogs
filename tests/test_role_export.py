import importlib.util
import sys
import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

MODULE_PATH = Path(__file__).parents[1] / "NHMisc" / "role_export.py"
SPEC = importlib.util.spec_from_file_location("nhmisc_role_export_test", MODULE_PATH)
role_export = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = role_export
SPEC.loader.exec_module(role_export)


class RoleExportTests(unittest.TestCase):
    def test_csv_is_sent_directly_and_safe_values_remain_verbatim(self):
        members = [
            role_export.ExportMember(1, "plain", "Plain Name"),
            role_export.ExportMember(2, "with.dot", "Comma, Name"),
        ]

        payload = role_export.build_role_export(members, upload_limit=10_000)

        self.assertEqual(payload.filename, "roleusers.csv")
        self.assertEqual(
            payload.data.decode("utf-8"),
            "user_id,username,display_name\n"
            "1,plain,Plain Name\n"
            '2,with.dot,"Comma, Name"\n',
        )

    def test_spreadsheet_formula_names_are_neutralized_without_losing_text(self):
        members = [
            role_export.ExportMember(1, "=cmd|'/c calc'!A0", "+1234"),
            role_export.ExportMember(2, "-minus", "@at"),
        ]

        payload = role_export.build_role_export(members, upload_limit=10_000)

        self.assertEqual(
            payload.data.decode("utf-8"),
            "user_id,username,display_name\n"
            "1,'=cmd|'/c calc'!A0,'+1234\n"
            "2,'-minus,'@at\n",
        )

    def test_csv_is_zipped_when_raw_bytes_exceed_upload_limit(self):
        members = [role_export.ExportMember(1, "same", "Same")] * 200

        payload = role_export.build_role_export(members, upload_limit=500)

        self.assertEqual(payload.filename, "roleusers.zip")
        self.assertLessEqual(len(payload.data), 500)
        with ZipFile(BytesIO(payload.data)) as archive:
            self.assertEqual(archive.namelist(), ["roleusers.csv"])
            csv_data = archive.read("roleusers.csv")
        self.assertEqual(csv_data.count(b"\n"), 201)

    def test_export_too_large_is_raised_when_zip_still_exceeds_limit(self):
        members = [role_export.ExportMember(1, "name", "Display")]

        with self.assertRaises(role_export.ExportTooLarge):
            role_export.build_role_export(members, upload_limit=1)


if __name__ == "__main__":
    unittest.main()
