"""
Template for Client_App/license_config.py.

This file IS committed to git - it holds no real secret or key, only
placeholders. The real license_config.py is generated per school by
build_for_school.py (see project root) and stays gitignored, so an
actual LICENSE_KEY or SIGNING_SECRET never lands in version control.
"""

LICENSE_KEY = "{{LICENSE_KEY}}"
SCHOOL_NAME = "{{SCHOOL_NAME}}"
SCHOOL_ABBREV = "{{SCHOOL_ABBREV}}"
LICENSE_SERVER_URL = "{{LICENSE_SERVER_URL}}"

# Full theme - matched one-to-one to the CSS custom properties style.css
# reads. Comparing two real schools' stylesheets showed this needs to be
# a full palette, not just brand/accent: different schools reskin the
# background, text, and muted tones too (e.g. a cool gray scheme vs a
# warm cream scheme), not only the brand color. Hex strings.
THEME_BG = "{{THEME_BG}}"
THEME_SURFACE = "{{THEME_SURFACE}}"
THEME_TEXT = "{{THEME_TEXT}}"
THEME_MUTED = "{{THEME_MUTED}}"
THEME_BRAND = "{{THEME_BRAND}}"
THEME_BRAND_DARK = "{{THEME_BRAND_DARK}}"
THEME_ACCENT = "{{THEME_ACCENT}}"
THEME_TINT = "{{THEME_TINT}}"
THEME_TINT_LIGHT = "{{THEME_TINT_LIGHT}}"
THEME_PRIMARY = "{{THEME_PRIMARY}}"
THEME_PRIMARY_DARK = "{{THEME_PRIMARY_DARK}}"

CHECK_INTERVAL_HOURS = 12
GRACE_PERIOD_DAYS = 10

SIGNING_SECRET = "{{SIGNING_SECRET}}"
