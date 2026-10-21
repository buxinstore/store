"""Starter storefront catalog shared by first-run setup and the seed command."""

import json
from pathlib import Path

from models import Category, Product, db

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path(__file__).resolve().parent / "assets_manifest.json"

CATEGORIES = [
    ("Microcontrollers & Development Boards", "microcontrollers-and-development-boards", "Development boards and microcontrollers for your next build.", "category-1"),
    ("Sensors & Modules", "sensors-and-modules", "Measure, detect, and connect with reliable sensor modules.", "category-2"),
    ("Electronic Components", "electronic-components", "Essential components for prototyping and repairs.", "category-3"),
    ("Power Supply & Conversion", "power-supply-and-conversion", "Power your projects with dependable converters.", "category-4"),
    ("Displays & Screens", "displays-and-screens", "Bring projects to life with compact displays.", "category-5"),
    ("Motors & Drivers", "motors-and-drivers", "Motion components and driver boards.", "category-6"),
    ("Tools & Accessories", "tools-and-accessories", "Practical tools for every maker.", "category-7"),
    ("Wires, Cables & Connectors", "wires-cables-and-connectors", "Wires and connectors for your circuits.", "category-8"),
    ("Robotics & Automation", "robotics-and-automation", "Parts for robotics and automation builds.", "product-5"),
    ("DIY Kits & More", "diy-kits-and-more", "Kits to learn, prototype, and make.", "product-1"),
]

PRODUCTS = [
    ("Arduino UNO R3", "arduino-uno-r3", "Microcontrollers & Development Boards", "product-1", 12.99, 15.99, 82, "4.8", True),
    ("ESP32 Development Board", "esp32-development-board", "Microcontrollers & Development Boards", "product-2", 8.99, 11.99, 54, "4.7", True),
    ("Arduino Compatible Board", "arduino-compatible-board", "Microcontrollers & Development Boards", "product-1", 10.49, None, 38, "4.6", False),
    ("Maker Starter Board", "maker-starter-board", "Microcontrollers & Development Boards", "product-2", 14.99, None, 22, "4.8", False),
    ("HC-SR04 Ultrasonic Sensor", "hc-sr04-ultrasonic-sensor", "Sensors & Modules", "product-4", 3.99, 6.99, 63, "4.8", True),
    ("DHT11 Temperature & Humidity Sensor", "dht11-temperature-humidity-sensor", "Sensors & Modules", "product-3", 4.99, 7.99, 31, "4.6", True),
    ("Ultrasonic Distance Module", "ultrasonic-distance-module", "Sensors & Modules", "category-2", 5.49, None, 28, "4.7", False),
    ("Digital Sensor Module", "digital-sensor-module", "Sensors & Modules", "product-3", 6.99, None, 19, "4.6", False),
    ("LED Assortment Kit", "led-assortment-kit", "Electronic Components", "category-3", 2.49, None, 75, "4.8", True),
    ("Breadboard 400 Tie Points", "breadboard-400-tie-points", "Electronic Components", "product-6", 1.99, None, 96, "4.6", True),
    ("Resistor & LED Starter Pack", "resistor-led-starter-pack", "Electronic Components", "category-3", 4.99, None, 42, "4.7", False),
    ("Jumper Wire Set", "jumper-wire-set", "Wires, Cables & Connectors", "category-8", 3.49, None, 67, "4.8", True),
    ("DC-DC Buck Converter", "dc-dc-buck-converter", "Power Supply & Conversion", "category-4", 5.99, None, 29, "4.8", True),
    ("0.96 inch OLED Display", "096-inch-oled-display", "Displays & Screens", "category-5", 6.99, None, 35, "4.8", True),
    ("L298N Motor Driver Module", "l298n-motor-driver-module", "Motors & Drivers", "product-5", 5.99, 9.99, 27, "4.7", True),
    ("Mini Gear Motor", "mini-gear-motor", "Motors & Drivers", "category-6", 4.49, None, 18, "4.6", False),
    ("Precision Screwdriver Set", "precision-screwdriver-set", "Tools & Accessories", "category-7", 8.99, None, 24, "4.8", True),
    ("Dupont Cable Bundle", "dupont-cable-bundle", "Wires, Cables & Connectors", "category-8", 4.99, None, 33, "4.7", False),
    ("Robotics Sensor Bundle", "robotics-sensor-bundle", "Robotics & Automation", "product-3", 11.99, None, 16, "4.8", True),
    ("Arduino Starter Kit", "arduino-starter-kit", "DIY Kits & More", "product-1", 24.99, None, 12, "4.9", True),
]


def seed_starter_catalog(update_existing=True):
    try:
        image_urls = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        image_urls = {}

    for name, slug, description, image_key in CATEGORIES:
        category = Category.query.filter_by(slug=slug).first()
        if category is None:
            category = Category(name=name, slug=slug, description=description)
            db.session.add(category)
        if update_existing or category.image_url is None:
            category.image_url = image_urls.get(image_key)

    db.session.flush()
    categories = {category.name: category for category in Category.query.all()}
    for name, slug, category_name, image_key, price, compare, stock, rating, featured in PRODUCTS:
        product = Product.query.filter_by(slug=slug).first()
        if product is None:
            product = Product(name=name, slug=slug, category=categories[category_name], price=price)
            db.session.add(product)
        if not update_existing and product.id is not None:
            continue
        product.name = name
        product.category = categories[category_name]
        product.description = f"{name} for electronics projects and prototyping."
        product.price = price
        product.compare_at_price = compare
        product.image_url = image_urls.get(image_key)
        product.stock = stock
        product.rating = rating
        product.is_active = True
        product.is_featured = featured

    db.session.commit()


if __name__ == "__main__":
    from app import app

    with app.app_context():
        seed_starter_catalog()
    print(f"Seeded {len(CATEGORIES)} categories and {len(PRODUCTS)} products.")
