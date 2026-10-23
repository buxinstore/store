import unittest
from io import BytesIO
from types import SimpleNamespace

from openpyxl import load_workbook

from product_excel import PRODUCT_HEADERS, build_product_template, parse_product_workbook, safe_external_url


class ProductExcelTests(unittest.TestCase):
    def setUp(self):
        self.category = SimpleNamespace(id=7, name="Sensors", slug="sensors")

    def workbook(self, *rows):
        workbook = load_workbook(build_product_template())
        sheet = workbook["Products"]
        for row in rows or (("Temperature Sensor", "SENSOR-1", "Sensors", "$4.25", 12, "https://images.example.com/a.jpg"),):
            sheet.append(row)
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def parse(self, content, existing=None):
        return parse_product_workbook(content, [self.category], existing or {})

    def test_template_is_one_sheet_with_only_the_six_product_columns(self):
        workbook = load_workbook(build_product_template(), read_only=True, data_only=True)
        self.assertEqual(workbook.sheetnames, ["Products"])
        headers = [cell.value for cell in next(workbook["Products"].iter_rows(min_row=1, max_row=1))]
        self.assertEqual(headers, PRODUCT_HEADERS)
        workbook.close()

    def test_legacy_products_and_categories_workbook_with_notes_column(self):
        workbook = load_workbook(build_product_template())
        products = workbook["Products"]
        products["H1"] = "How to use"
        products["H2"] = "Notes are ignored by the product importer."
        categories = workbook.create_sheet("Categories")
        categories.append(["Category"])
        categories.append(["Sensors"])
        products.append(["Imported Sensor", "SENSOR-2", "Sensors", 4, 2, None, None, "A row note"])
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        result = self.parse(output.getvalue())
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["products"][0]["name"], "Imported Sensor")

    def test_product_and_optional_image_are_parsed(self):
        result = self.parse(self.workbook())
        self.assertEqual(result["errors"], [])
        product = result["products"][0]
        self.assertEqual(product["name"], "Temperature Sensor")
        self.assertEqual(product["sku"], "SENSOR-1")
        self.assertEqual(product["category_id"], 7)
        self.assertEqual(product["price"], "4.25")
        self.assertEqual(product["stock"], 12)
        self.assertEqual(product["primary_image_url"], "https://images.example.com/a.jpg")

    def test_blank_image_is_valid_and_kept_unprovided(self):
        result = self.parse(self.workbook(("Sensor", "S-2", "Sensors", 3, 1, None)))
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["products"][0]["primary_image_url"], "")
        self.assertNotIn("Product image", result["products"][0]["provided"])

    def test_duplicate_sku_and_invalid_category_reported(self):
        result = self.parse(self.workbook(
            ("Sensor", "S-1", "Sensors", 3, 1, None),
            ("Other", "s-1", "Missing", 2, 4, None),
        ))
        messages = " ".join(error["message"] for error in result["errors"])
        self.assertIn("Duplicate SKU", messages)
        self.assertIn("does not match an existing category", messages)

    def test_sku_updates_existing_record_and_not_duplicate(self):
        result = self.parse(self.workbook(), {"sensor-1": object()})
        self.assertEqual(result["update_count"], 1)
        self.assertEqual(result["new_count"], 0)

    def test_invalid_private_image_url_is_rejected(self):
        result = self.parse(self.workbook(("Sensor", "S-1", "Sensors", 3, 1, "https://127.0.0.1/private.png")))
        self.assertTrue(any("Product image" in error["message"] for error in result["errors"]))

    def test_existing_media_validator_name_remains_available(self):
        self.assertTrue(safe_external_url("https://images.example.com/a.png", "image"))
        self.assertTrue(safe_external_url("https://youtu.be/abcdefghijk", "video"))
        self.assertFalse(safe_external_url("https://example.com/video.mp4", "video"))

    def test_formula_cells_are_rejected(self):
        content = self.workbook()
        workbook = load_workbook(BytesIO(content))
        workbook["Products"]["A2"] = "=HYPERLINK(\"https://example.com\",\"click\")"
        result = BytesIO()
        workbook.save(result)
        with self.assertRaisesRegex(ValueError, "Formula cells are not allowed"):
            self.parse(result.getvalue())

    def test_malformed_workbook_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Could not read"):
            self.parse(b"not an xlsx")


if __name__ == "__main__":
    unittest.main()
