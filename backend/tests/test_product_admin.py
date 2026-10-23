import os
import unittest
from io import BytesIO

from openpyxl import load_workbook

from product_excel import build_product_template


class ProductAdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env_keys = ("DATABASE_URL", "ADMIN_USERNAME", "ADMIN_PASSWORD", "SECRET_KEY")
        cls.previous = {key: os.environ.get(key) for key in cls.env_keys}
        os.environ.update(DATABASE_URL="sqlite://", ADMIN_USERNAME="product-test-admin",
                          ADMIN_PASSWORD="product-test-password", SECRET_KEY="product-test-secret")
        from app import create_app
        from models import db
        cls.db = db
        cls.app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                              "SECRET_KEY": "product-test-secret"})

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        from models import Category
        with self.app.app_context():
            self.db.drop_all()
            self.db.create_all()
            category = Category(name="Sensors", slug="sensors")
            self.db.session.add(category)
            self.db.session.commit()
            self.category_id = category.id
        self.client = self.app.test_client()
        login = self.client.post("/api/admin/login", json={"username": "product-test-admin", "password": "product-test-password"})
        self.assertEqual(login.status_code, 200)
        self.headers = {"Authorization": "Bearer " + login.get_json()["token"]}

    def create(self, **overrides):
        payload = {"name": "Temperature Sensor", "sku": "SENSOR-1", "category_id": self.category_id,
                   "price": "4.25", "stock": 5, "status": "active", "weight_kg": "0.25"}
        payload.update(overrides)
        return self.client.post("/api/admin/products", headers=self.headers, json=payload)

    def make_workbook(self, *, duplicate=False, image=True, video=True):
        output = build_product_template()
        workbook = load_workbook(output)
        sheet = workbook["Products"]
        sheet.append(["Imported Sensor", "IMPORTED-1", "imported-sensor", "Short", "Full", "Sensors", "", "Maker", "sensor", 3.5, None, "USD", 10, "TRUE", "Active", "TRUE", 0.2, "kg", "TRUE", "MPN-1", "Model-1", "Range: 0 to 100", "", "", "", ""])
        if duplicate:
            sheet.append(["Duplicate", "imported-1", "duplicate", "", "", "Sensors", "", "", "", 2, None, "USD", 1, "TRUE", "Draft", "FALSE", 0.1, "kg", "TRUE", "", "", "", "", "", "", ""])
        media = workbook["Images and Videos"]
        if image:
            media.append(["IMPORTED-1", "Image", "https://images.example.com/front.jpg", "Front", "TRUE", 0])
            media.append(["IMPORTED-1", "Image", "https://images.example.com/back.jpg", "Back", "FALSE", 1])
        if video:
            media.append(["IMPORTED-1", "Video", "https://youtu.be/abcdefghijk", "Demo", "FALSE", 0])
        result = BytesIO()
        workbook.save(result)
        return result.getvalue()

    def preview(self, content):
        return self.client.post("/api/admin/products/excel-preview", headers=self.headers,
                                data={"file": (BytesIO(content), "products.xlsx")}, content_type="multipart/form-data")

    def test_manual_create_edit_deactivate_and_public_visibility(self):
        created = self.create(short_description="Test module", is_featured=True,
                              details={"track_inventory": False, "shipping_required": True},
                              specifications={"Voltage": "5V"})
        self.assertEqual(created.status_code, 201, created.get_json())
        product = created.get_json()["item"]
        self.assertEqual(product["specifications"], {"Voltage": "5V"})
        self.assertEqual(self.client.get(f"/api/products/slug/{product['slug']}").status_code, 200)
        updated = self.client.patch(f"/api/admin/products/{product['id']}", headers=self.headers,
                                    json={"status": "inactive", "price": "5.00"})
        self.assertEqual(updated.status_code, 200, updated.get_json())
        self.assertEqual(self.client.get(f"/api/products/slug/{product['slug']}").status_code, 404)
        archived = self.client.delete(f"/api/admin/products/{product['id']}", headers=self.headers)
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(archived.get_json()["item"]["status"], "archived")

    def test_create_validation_and_case_insensitive_sku_uniqueness(self):
        self.assertEqual(self.create(price=-1).status_code, 400)
        self.assertEqual(self.create().status_code, 201)
        duplicate = self.create(name="Other", slug="other", sku="sensor-1")
        self.assertEqual(duplicate.status_code, 409)

    def test_template_preview_import_media_and_repeat_import_by_sku(self):
        template = self.client.get("/api/admin/products/excel-template", headers=self.headers)
        self.assertEqual(template.status_code, 200)
        self.assertIn("spreadsheetml", template.mimetype)
        preview = self.preview(self.make_workbook())
        self.assertEqual(preview.status_code, 201, preview.get_json())
        result = self.client.post("/api/admin/products/excel-confirm", headers=self.headers,
                                  json={"import_id": preview.get_json()["import_id"], "confirm_import": True})
        self.assertEqual(result.status_code, 200, result.get_json())
        imported = self.client.get("/api/admin/products?q=IMPORTED-1", headers=self.headers).get_json()["items"][0]
        self.assertEqual(len(imported["media"]), 3)
        self.assertTrue(imported["media"][0]["is_primary"])
        repeated = self.preview(self.make_workbook())
        self.assertEqual(repeated.get_json()["preview"]["existing_products_to_update"], 1)
        result2 = self.client.post("/api/admin/products/excel-confirm", headers=self.headers,
                                   json={"import_id": repeated.get_json()["import_id"], "confirm_import": True})
        self.assertEqual(result2.status_code, 200, result2.get_json())
        self.assertEqual(self.client.get("/api/admin/products?q=IMPORTED-1", headers=self.headers).get_json()["count"], 1)

    def test_invalid_rows_block_staging_and_preserve_existing_media(self):
        self.assertEqual(self.create(sku="KEEP-1", media=[{"type": "image", "url": "https://images.example.com/keep.jpg", "is_primary": True}]).status_code, 201)
        invalid = self.preview(self.make_workbook(duplicate=True))
        self.assertGreater(invalid.get_json()["preview"]["invalid_rows"], 0)
        self.assertIsNone(invalid.get_json()["import_id"])
        existing = self.client.get("/api/admin/products?q=KEEP-1", headers=self.headers).get_json()["items"][0]
        self.assertEqual(existing["media"][0]["url"], "https://images.example.com/keep.jpg")

    def test_product_list_search_category_status_availability_and_pagination(self):
        self.create(name="Alpha", sku="ALPHA", slug="alpha", stock=0)
        self.create(name="Beta", sku="BETA", slug="beta", status="draft")
        response = self.client.get("/api/admin/products?q=ALPHA&availability=out_of_stock&page=1&per_page=1", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 1)
        self.assertEqual(response.get_json()["pages"], 1)
        self.assertEqual(response.get_json()["items"][0]["sku"], "ALPHA")

    def test_admin_routes_require_authentication_and_categories_do_not_duplicate(self):
        self.assertEqual(self.client.get("/api/admin/products/excel-template").status_code, 401)
        self.assertEqual(self.client.post("/api/admin/categories", headers=self.headers,
                                          json={"name": "sensors"}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
