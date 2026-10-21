"""Upload the reference storefront image crops to Cloudinary and save a public URL manifest."""

import json
import os
from pathlib import Path

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
MANIFEST = Path(__file__).with_name("assets_manifest.json")


def main():
    if not os.getenv("CLOUDINARY_URL"):
        raise SystemExit("Set CLOUDINARY_URL in backend/.env before uploading.")
    cloudinary.config(secure=True)
    manifest = {}
    for path in sorted(ASSETS.glob("*.jpg")):
        result = cloudinary.uploader.upload(
            str(path),
            folder="electrohub/storefront",
            public_id=path.stem,
            overwrite=True,
            resource_type="image",
        )
        manifest[path.stem] = result["secure_url"]
        print(f"Uploaded {path.name}")
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Saved Cloudinary URL manifest to {MANIFEST}")


if __name__ == "__main__":
    main()
