"""
Builds a school-specific Bookroom client .exe from the single shared
Client_App/ source, instead of maintaining a separate full copy of the
codebase per school.

Usage:
    python build_for_school.py mona_high_school

Reads school_profiles/<name>.json for that school's license_key,
school_name, school_abbrev, and full theme (11 colors - see
THEME_KEYS below), pulls the shared signing secret from the
LICENSE_SIGNING_SECRET environment variable (never stored in git),
writes Client_App/license_config.py and Client_App/static/manifest.json
from their templates, copies that school's crest image(s) into
Client_App/static/images/ (same destination filenames every time - only
the source files change), builds with PyInstaller, then copies the
output to a school-named .exe so each build doesn't overwrite the last
one.

Each school needs, alongside its .json profile in school_profiles/:
  <name>_crest.png       - required (header, favicon, watermarks).
                            Any reasonable size/aspect ratio.
  <name>_crest_icon.png  - optional, a square 512x512 variant for the
                            PWA manifest icon; falls back to
                            <name>_crest.png if not provided (manifest
                            will still declare 512x512 regardless, so a
                            non-512 fallback will look slightly off in
                            app icon contexts specifically - fine for
                            everything else).

Run this from the project root (same folder as app.spec).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
CLIENT_DIR = REPO_ROOT / "Client_App"
PROFILES_DIR = REPO_ROOT / "school_profiles"
TEMPLATE_PATH = CLIENT_DIR / "license_config.template.py"
OUTPUT_PATH = CLIENT_DIR / "license_config.py"
MANIFEST_TEMPLATE_PATH = CLIENT_DIR / "static" / "manifest.json.template"
MANIFEST_OUTPUT_PATH = CLIENT_DIR / "static" / "manifest.json"

# Shared across every school - the same Railway URL for all of them.
# Update here once if it ever changes, rather than per profile.
LICENSE_SERVER_URL = "https://your-service.up.railway.app/check_license"

# Must match the "theme" object keys expected in each school_profiles/*.json,
# and the THEME_* names in license_config.template.py / manifest.json.template.
THEME_KEYS = [
    "bg", "surface", "text", "muted", "brand", "brand_dark",
    "accent", "tint", "tint_light", "primary", "primary_dark",
]


def main():
    if len(sys.argv) != 2:
        available = ", ".join(p.stem for p in PROFILES_DIR.glob("*.json"))
        print("Usage: python build_for_school.py <profile_name>")
        print(f"Available profiles: {available or '(none found)'}")
        sys.exit(1)

    profile_name = sys.argv[1]
    profile_path = PROFILES_DIR / f"{profile_name}.json"
    if not profile_path.exists():
        print(f"No profile found at {profile_path}")
        sys.exit(1)

    profile = json.loads(profile_path.read_text())
    license_key = profile["license_key"]
    school_name = profile["school_name"]
    school_abbrev = profile["school_abbrev"]

    try:
        theme = {key: profile["theme"][key] for key in THEME_KEYS}
    except KeyError as missing:
        print(f"Profile {profile_path} is missing theme key {missing} "
              f"(needs all of: {', '.join(THEME_KEYS)})")
        sys.exit(1)

    crest_path = PROFILES_DIR / f"{profile_name}_crest.png"
    if not crest_path.exists():
        print(f"No crest image found at {crest_path}")
        sys.exit(1)

    crest_icon_path = PROFILES_DIR / f"{profile_name}_crest_icon.png"
    if not crest_icon_path.exists():
        crest_icon_path = crest_path  # fall back to the general crest

    signing_secret = os.environ.get("LICENSE_SIGNING_SECRET")
    if not signing_secret:
        print(
            "Set LICENSE_SIGNING_SECRET as an environment variable before "
            "building - it must match the value set in Railway."
        )
        sys.exit(1)

    template = TEMPLATE_PATH.read_text()
    generated = (
        template
        .replace("{{LICENSE_SERVER_URL}}", LICENSE_SERVER_URL)
        .replace("{{LICENSE_KEY}}", license_key)
        .replace("{{SCHOOL_NAME}}", school_name)
        .replace("{{SCHOOL_ABBREV}}", school_abbrev)
        .replace("{{SIGNING_SECRET}}", signing_secret)
    )
    for key in THEME_KEYS:
        generated = generated.replace(f"{{{{THEME_{key.upper()}}}}}", theme[key])
    OUTPUT_PATH.write_text(generated)
    print(f"Wrote {OUTPUT_PATH} for {school_name}.")

    crest_dest = CLIENT_DIR / "static" / "images" / "crest.png"
    shutil.copy2(crest_path, crest_dest)
    print(f"Copied crest to {crest_dest}.")

    crest_icon_dest = CLIENT_DIR / "static" / "images" / "crest_icon.png"
    shutil.copy2(crest_icon_path, crest_icon_dest)
    print(f"Copied manifest icon to {crest_icon_dest}.")

    # json.dumps(...)[1:-1] gives the properly-escaped inner text for a
    # JSON string (handles a stray " or \ safely) - plain str.replace()
    # alone could produce invalid JSON otherwise.
    manifest_template = MANIFEST_TEMPLATE_PATH.read_text()
    manifest_generated = (
        manifest_template
        .replace("{{SCHOOL_NAME_TITLE}}", json.dumps(school_name.title())[1:-1])
        .replace("{{SCHOOL_ABBREV}}", json.dumps(school_abbrev)[1:-1])
        .replace("{{THEME_BG}}", theme["bg"])
        .replace("{{THEME_TINT}}", theme["tint"])
    )
    MANIFEST_OUTPUT_PATH.write_text(manifest_generated)
    print(f"Wrote {MANIFEST_OUTPUT_PATH} for {school_name}.")

    print("Building with PyInstaller...")
    subprocess.run(["pyinstaller", "app.spec"], cwd=REPO_ROOT, check=True)

    built_exe = REPO_ROOT / "dist" / "app.exe"
    if not built_exe.exists():
        print("Build finished but dist/app.exe wasn't found - check the PyInstaller output above.")
        sys.exit(1)

    slug = school_name.replace(" ", "_")
    final_path = REPO_ROOT / "dist" / f"{slug}_Bookroom.exe"
    shutil.copy2(built_exe, final_path)
    print(f"Done: {final_path}")


if __name__ == "__main__":
    main()
