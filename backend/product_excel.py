"""Validated product import workbook template and parser."""
from copy import copy
from io import BytesIO
import ipaddress
import math
import re
import zipfile
from urllib.parse import urlsplit

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation


PRODUCT_HEADERS = [
    "Product Name", "SKU", "Slug", "Short Description", "Full Description", "Category",
    "Subcategory", "Brand", "Tags", "Selling Price", "Original Price", "Currency",
    "Stock Quantity", "Track Inventory", "Product Status", "Featured", "Product Weight",
    "Weight Unit", "Shipping Required", "Manufacturer Part Number", "Model Number",
    "Specifications", "Datasheet URL", "Warranty Information", "Country of Origin", "Primary Image URL",
]
MEDIA_HEADERS = ["Product SKU", "Media Type", "Media URL", "Title or Alt Text", "Primary Image", "Display Order"]
MAX_XLSX_BYTES = 4 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 24 * 1024 * 1024
MAX_PRODUCTS = 1500
MAX_MEDIA = 6000
ALLOWED_VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com", "player.vimeo.com"}
DETAIL_KEYS = ("manufacturer_part_number", "model_number", "specifications", "datasheet_url", "warranty_information", "country_of_origin", "track_inventory", "shipping_required", "weight_unit")


def safe_external_url(value, kind):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
            return ""
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return ""
        except ValueError:
            if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
                return ""
        if kind == "video" and host not in ALLOWED_VIDEO_HOSTS:
            return ""
        return value[:2000]
    except ValueError:
        return ""


def _slug(value):
    value = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return value[:200]


def build_product_template():
    workbook = Workbook()
    instructions = workbook.active
    instructions.title = "Instructions"
    for row in [
        ["Buxin Store Product Import"],
        ["Fill Products and optionally Images and Videos, then upload the .xlsx file in Admin → Products."],
        ["Only the supported USD currency is accepted. Prices are non-negative; blank optional cells preserve existing values."],
        ["Use Draft, Active, or Inactive. Track Inventory, Featured, Shipping Required, and Primary Image use TRUE or FALSE."],
        ["Category must exactly match an existing store category (case-insensitive). SKU updates an existing product; blank SKU uses the product name and slug."],
        ["Product Weight must be in kilograms. Weight Unit accepts kg or g; grams convert to kg for existing shipping calculations."],
        ["Specifications use plain text, one key: value pair per line. Datasheet and media URLs must use HTTPS."],
        ["Videos are accepted only from YouTube or Vimeo. Media records link to a product by SKU."],
        ["Blank image/video cells preserve existing media. To replace media, provide at least one new row; removal remains available in the product editor."],
        ["Optional columns not supported by the store (including cost and tax fields) are not included and will not be imported."],
        ["The example rows are labeled EXAMPLE - DELETE BEFORE IMPORT and are ignored by the importer."],
        ["Products without images or videos are allowed. External media is stored as URLs and is never downloaded by the server."],
    ]:
        instructions.append(row)
    products = workbook.create_sheet("Products")
    products.append(PRODUCT_HEADERS)
    products.append(["EXAMPLE - DELETE BEFORE IMPORT", "EXAMPLE-SKU", "example-product", "Example only", "Delete this row", "Existing Category", None, None, None, 1.00, None, "USD", 1, "TRUE", "Draft", "FALSE", 0.5, "kg", "TRUE", None, None, "Voltage: 5V", None, None, None, None])
    media = workbook.create_sheet("Images and Videos")
    media.append(MEDIA_HEADERS)
    media.append(["EXAMPLE-SKU", "Image", "https://example.com/example.jpg", "Example image (delete this row)", "TRUE", 1])
    for sheet in (products, media):
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="17365D")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        sheet.row_dimensions[1].height = 32
        for column in sheet.columns:
            letter = column[0].column_letter
            sheet.column_dimensions[letter].width = min(max(max(len(str(cell.value or "")) for cell in column[:30]) + 2, 14), 34)
        for cell in sheet[2]:
            cell.fill = PatternFill("solid", fgColor="FFF2CC")
    for row in range(3, products.max_row + 1):
        for column in (10, 11):
            products.cell(row, column).number_format = '"$"#,##0.00'
        products.cell(row, 17).number_format = "0.000"
    status = DataValidation(type="list", formula1='"Draft,Active,Inactive"', allow_blank=True)
    products.add_data_validation(status)
    status.add("O2:O1500")
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def parse_product_workbook(data, categories, existing_by_sku):
    if not isinstance(data, bytes) or not data or len(data) > MAX_XLSX_BYTES:
        raise ValueError("Workbook must be a non-empty .xlsx file no larger than 4 MB")
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            if len(archive.namelist()) > 100 or sum(item.file_size for item in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Workbook expands beyond the allowed size")
            if any(item.file_size and item.compress_size == 0 for item in archive.infolist()):
                raise ValueError("Workbook contains an invalid compressed entry")
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=False)
    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        raise ValueError("Could not read the .xlsx workbook") from exc
    if "Products" not in workbook.sheetnames or "Images and Videos" not in workbook.sheetnames:
        raise ValueError("Workbook must include Products and Images and Videos worksheets")
    category_map = {}
    existing_by_sku = {str(key).strip().casefold(): value for key, value in (existing_by_sku or {}).items()}
    for category in categories:
        category_map[str(category.name).strip().casefold()] = category
        category_map[str(category.slug).strip().casefold()] = category
    product_sheet = workbook["Products"]
    for sheet in (product_sheet, workbook["Images and Videos"]):
        for row in sheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    workbook.close()
                    raise ValueError(f"Formula cells are not allowed ({sheet.title}!{cell.coordinate})")
    headers = [str(cell.value or "").strip() for cell in next(product_sheet.iter_rows(min_row=1, max_row=1))]
    if len(set(headers)) != len(headers) or set(headers) != set(PRODUCT_HEADERS):
        raise ValueError("Products worksheet headers do not match the downloaded template")
    col = {header: headers.index(header) for header in PRODUCT_HEADERS}
    rows, errors, workbook_skus, detected = [], [], set(), 0
    if product_sheet.max_row > MAX_PRODUCTS + 1:
        raise ValueError("Workbook contains more than 1,500 product rows")
    for row_number, values in enumerate(product_sheet.iter_rows(min_row=2, values_only=True), start=2):
        row = {name: (values[index] if index < len(values) else None) for name, index in col.items()}
        if not any(value not in (None, "") for value in row.values()):
            continue
        if str(row["Product Name"] or "").strip().upper() == "EXAMPLE - DELETE BEFORE IMPORT":
            continue
        detected += 1
        row_errors = []
        name = str(row["Product Name"] or "").strip()
        sku = str(row["SKU"] or "").strip().upper() or None
        category_name = str(row["Category"] or "").strip()
        category = category_map.get(category_name.casefold())
        if not name:
            row_errors.append("Product Name is required")
        if len(name) > 180:
            row_errors.append("Product Name exceeds 180 characters")
        if not category:
            row_errors.append(f"Category '{category_name or '(blank)'}' does not match an existing category")
        for key, label in (("Selling Price", "Selling Price"), ("Stock Quantity", "Stock Quantity")):
            try:
                number = float(row[key])
                if not math.isfinite(number) or number < 0 or (key == "Selling Price" and number > 99999999.99):
                    raise ValueError
                if key == "Stock Quantity" and int(number) != number:
                    raise ValueError
                if key == "Stock Quantity" and number > 2147483647:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                row_errors.append(f"{label} must be a non-negative number" + (" (whole number)" if key == "Stock Quantity" else ""))
        currency = str(row["Currency"] or "USD").strip().upper()
        if currency != "USD":
            row_errors.append("Currency must be USD to match the store checkout")
        status = str(row["Product Status"] or "Active").strip().lower()
        if status not in {"draft", "active", "inactive"}:
            row_errors.append("Product Status must be Draft, Active, or Inactive")
        slug = _slug(row["Slug"] or name)
        for field in ("Datasheet URL",):
            if row[field] and not safe_external_url(row[field], "image"):
                row_errors.append(f"{field} must be a safe HTTPS URL")
        for field in ("Featured", "Track Inventory", "Shipping Required"):
            value = row[field]
            if value not in (None, "") and str(value).strip().lower() not in {"true", "false", "yes", "no", "1", "0"}:
                row_errors.append(f"{field} must be TRUE or FALSE")
        weight = row["Product Weight"]
        if weight not in (None, ""):
            try:
                weight_value = float(weight)
                unit = str(row["Weight Unit"] or "kg").strip().lower()
                if unit not in {"kg", "g"} or weight_value <= 0:
                    raise ValueError
                if unit == "g":
                    weight_value /= 1000
                if weight_value > 1000:
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                row_errors.append("Product Weight must be positive and Weight Unit must be kg or g")
                weight_value = None
        else:
            weight_value = None
        compare = row["Original Price"]
        if compare not in (None, ""):
            try:
                original_price = float(compare)
                selling_price = float(row["Selling Price"])
                if not math.isfinite(original_price) or original_price < selling_price or original_price > 99999999.99:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                row_errors.append("Original Price must be a finite USD amount at least as high as Selling Price")
        if sku and sku in workbook_skus:
            row_errors.append(f"Duplicate SKU '{sku}' in workbook")
        if sku:
            workbook_skus.add(sku)
        if row_errors:
            errors.extend({"row": row_number, "message": message} for message in row_errors)
            continue
        specs = {}
        for line in str(row["Specifications"] or "").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                if key.strip() and value.strip():
                    specs[key.strip()[:100]] = value.strip()[:500]
        truth = lambda value: str(value or "false").strip().lower() in {"true", "yes", "1"}
        detail_fields = {"Manufacturer Part Number": "manufacturer_part_number", "Model Number": "model_number",
                         "Datasheet URL": "datasheet_url",
                         "Warranty Information": "warranty_information", "Country of Origin": "country_of_origin",
                         "Track Inventory": "track_inventory", "Shipping Required": "shipping_required", "Weight Unit": "weight_unit"}
        details = {}
        for source, target in detail_fields.items():
            if row[source] not in (None, ""):
                if target == "specifications":
                    details[target] = specs
                elif target in {"track_inventory", "shipping_required"}:
                    details[target] = truth(row[source])
                else:
                    details[target] = str(row[source]).strip().lower() if target == "weight_unit" else str(row[source]).strip()
        primary_url = safe_external_url(row["Primary Image URL"], "image") if row["Primary Image URL"] else ""
        if row["Primary Image URL"] and not primary_url:
            errors.append({"row": row_number, "message": "Primary Image URL must be a safe HTTPS URL"})
            continue
        rows.append({"row": row_number, "provided": [key for key, value in row.items() if value not in (None, "")], "name": name[:180], "sku": sku, "slug": slug,
                     "short_description": str(row["Short Description"] or "")[:500],
                     "description": str(row["Full Description"] or ""), "category_id": category.id,
                     "category_name": category.name, "subcategory": str(row["Subcategory"] or "")[:120],
                     "brand": str(row["Brand"] or "")[:120],
                     "price": str(row["Selling Price"]), "compare_at_price": str(compare) if compare not in (None, "") else None,
                     "stock": int(float(row["Stock Quantity"])), "weight_kg": str(weight_value) if weight_value is not None else None,
                     "is_active": status == "active", "status": status, "is_featured": truth(row["Featured"]),
                     "details": details, "primary_image_url": primary_url,
                     "specifications": specs if row["Specifications"] not in (None, "") else None,
                     "tags": [tag.strip() for tag in str(row["Tags"] or "").split(",") if tag.strip()][:50] if row["Tags"] not in (None, "") else None})

    media_sheet = workbook["Images and Videos"]
    media_headers = [str(cell.value or "").strip() for cell in next(media_sheet.iter_rows(min_row=1, max_row=1))]
    if len(set(media_headers)) != len(media_headers) or set(media_headers) != set(MEDIA_HEADERS):
        raise ValueError("Images and Videos worksheet headers do not match the downloaded template")
    mcol = {header: media_headers.index(header) for header in MEDIA_HEADERS}
    known_skus = {sku.casefold() for sku in workbook_skus} | set(existing_by_sku)
    media_rows, primary_skus = [], set()
    if media_sheet.max_row > MAX_MEDIA + 1:
        raise ValueError("Workbook contains more than 6,000 media rows")
    for row_number, values in enumerate(media_sheet.iter_rows(min_row=2, values_only=True), start=2):
        row = {name: (values[index] if index < len(values) else None) for name, index in mcol.items()}
        if not any(value not in (None, "") for value in row.values()):
            continue
        sku = str(row["Product SKU"] or "").strip().upper()
        if sku == "EXAMPLE-SKU":
            continue
        row_errors = []
        if not sku or sku.casefold() not in known_skus:
            row_errors.append(f"Media SKU '{sku or '(blank)'}' is not present in this workbook or the store")
        kind = str(row["Media Type"] or "").strip().lower()
        if kind not in {"image", "video"}:
            row_errors.append("Media Type must be Image or Video")
        primary = str(row["Primary Image"] or "false").strip().lower() in {"true", "yes", "1"}
        if primary and kind == "video":
            row_errors.append("Only an image can be marked as the primary image")
        if primary and kind == "image":
            if sku.casefold() in primary_skus:
                row_errors.append(f"SKU '{sku}' has more than one primary image")
            primary_skus.add(sku.casefold())
        url = safe_external_url(row["Media URL"], kind) if kind in {"image", "video"} else ""
        if not url:
            row_errors.append("Media URL must be HTTPS and videos must use YouTube or Vimeo")
        try:
            order = int(row["Display Order"] or 0)
            if order < 0 or order > 10000:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            row_errors.append("Display Order must be a non-negative whole number")
            order = 0
        if row_errors:
            errors.extend({"row": row_number, "sheet": "Images and Videos", "message": message} for message in row_errors)
        else:
            media_rows.append({"sku": sku, "type": kind, "url": url, "title": str(row["Title or Alt Text"] or "")[:300],
                               "primary": primary, "order": order})
    workbook.close()
    if detected == 0 and not media_rows:
        raise ValueError("Workbook does not contain any product rows or valid media rows")
    return {"products": rows, "media": media_rows, "errors": errors,
            "rows_detected": detected, "new_count": sum(1 for row in rows if not row["sku"] or row["sku"].casefold() not in existing_by_sku),
            "update_count": sum(1 for row in rows if row["sku"] and row["sku"].casefold() in existing_by_sku)}
