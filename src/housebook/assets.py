"""Build the committed, offline dashboard assets from pinned packages."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = Path(__file__).resolve().parent / "static"

VENDOR_FILES = {
    "node_modules/vue/dist/vue.global.prod.js":
        "vendor/vue.global.prod.js",
    "node_modules/chart.js/dist/chart.umd.min.js":
        "vendor/chart.umd.min.js",
    "node_modules/chartjs-chart-sankey/dist/chartjs-chart-sankey.min.js":
        "vendor/chartjs-chart-sankey.min.js",
}

LICENSE_FILES = {
    "node_modules/vue/LICENSE": "vendor/vue.LICENSE.txt",
    "node_modules/chart.js/LICENSE.md": "vendor/chartjs.LICENSE.txt",
    "node_modules/chartjs-chart-sankey/LICENSE":
        "vendor/chartjs-chart-sankey.LICENSE.txt",
    "node_modules/tailwindcss/LICENSE": "vendor/tailwindcss.LICENSE.txt",
}


def _require(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(
            f"missing UI build input: {path}; "
            "run 'npm install --ignore-scripts' first"
        )
    return path


def main() -> None:
    css_dir = STATIC_ROOT / "css"
    vendor_dir = STATIC_ROOT / "vendor"
    css_dir.mkdir(parents=True, exist_ok=True)
    vendor_dir.mkdir(parents=True, exist_ok=True)

    tailwind_name = "tailwindcss.cmd" if os.name == "nt" else "tailwindcss"
    tailwind = _require(
        PROJECT_ROOT / "node_modules" / ".bin" / tailwind_name
    )
    css_input = _require(STATIC_ROOT / "src" / "dashboard.css")
    css_output = css_dir / "dashboard.css"
    subprocess.run(
        [
            str(tailwind),
            "-i", str(css_input),
            "-o", str(css_output),
            "--minify",
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )

    for source, destination in {**VENDOR_FILES, **LICENSE_FILES}.items():
        source_path = _require(PROJECT_ROOT / source)
        destination_path = STATIC_ROOT / destination
        shutil.copyfile(source_path, destination_path)

    print(
        f"Built dashboard CSS and {len(VENDOR_FILES)} pinned vendor assets."
    )


if __name__ == "__main__":
    main()
