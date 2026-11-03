"""Tiny Flask front-end for scanimage (SANE).

Scans are saved to ~/Scans (change SCAN_DIR below).
"""

import argparse
import os
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

SCAN_DIR = Path(os.getenv("SCAN_DIR", "/scans"))
DEFAULT_DEVICE = os.getenv("DEFAULT_DEVICE")
PAPER_HEIGHT_MM = {"auto": None, "a4": 297, "letter": 279.4, "legal": 355.6}
RESOLUTIONS = {"200", "300", "400", "600"}
MODES = {"Color", "Gray"}
OUTPUTS = {"pdf", "pdfpages", "png"}
SOURCE = "ADF Front"  # exact name reported by this scanner

app = Flask(__name__)
lock = threading.Lock()
state = {"running": False, "log": "", "files": [], "error": None}
known_devices = []
# Pages scanned one at a time, waiting to be merged by /api/finish
session = {"stamp": None, "fmt": None, "output": None, "pages": []}


def list_devices() -> list[str]:
    """Return device names reported by `scanimage -f %d%n`.
    This gets the available devices in a nice format (device name followed by a new line)"""
    global known_devices
    try:
        out = subprocess.run(
            ["scanimage", "-f", "%d%n"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except OSError, subprocess.TimeoutExpired:
        out = ""
    known_devices = [line.strip() for line in out.splitlines() if line.strip()]
    return known_devices


def scanimage_supports_pdf() -> bool:
    """True if this scanimage build lists pdf among its --format choices."""
    try:
        out = subprocess.run(
            ["scanimage", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except OSError, subprocess.TimeoutExpired:
        return False
    return any("--format" in line and "pdf" in line for line in out.splitlines())


PDF_SUPPORTED = scanimage_supports_pdf()


def merge_pdfs(pages, dest) -> bool:
    """scanimage writes one PDF per page, so join them. Returns True on success."""
    try:
        from pypdf import PdfWriter

        writer = PdfWriter()
        for page in pages:
            writer.append(page)
        with open(dest, "wb") as fh:
            writer.write(fh)
        return True
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        log(f"pypdf merge failed: {exc}\n")
    if shutil.which("pdfunite"):
        return (
            subprocess.run(["pdfunite", *pages, str(dest)], check=False).returncode == 0
        )
    return False


def log(msg):
    with lock:
        state["log"] += msg


def choose_format(output: str) -> str:
    """Use PDF if it was requested and this scanimage supports it, else PNG."""
    if output == "png":
        return "png"
    if PDF_SUPPORTED:
        return "pdf"
    log("This scanimage has no PDF support, saving PNGs instead.\n")
    return "png"


def build_command(
    device: str, resolution: str, mode: str, paper: str, fmt: str, stamp: str
) -> list[str]:
    """Build the scanimage batch command line."""
    cmd = [
        "scanimage",
        "-d",
        device,
        "--source",
        SOURCE,
        "--mode",
        mode,
        "--resolution",
        resolution,
        "--format",
        fmt,
        f"--batch={SCAN_DIR / f'scan_{stamp}_%03d.{fmt}'}",
    ]
    height = PAPER_HEIGHT_MM[paper]
    if height:
        cmd += ["-y", str(height)]
    return cmd


def stream_command(cmd: list[str]) -> None:
    """Run cmd and copy its output into the log.

    The exit code is deliberately ignored: scanimage exits non-zero when the
    feeder runs out of pages, which is normal.
    """
    log("$ " + " ".join(cmd) + "\n")
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    ) as proc:
        if proc.stdout is None:
            raise RuntimeError("scanimage stdout pipe was not created")
        for line in proc.stdout:
            log(line)


def finalise(pages: list[Path], output: str, fmt: str, stamp: str) -> list[str]:
    """Merge per-page PDFs if one PDF was requested. Returns the final file names."""
    if output != "pdf" or fmt != "pdf":
        return [p.name for p in pages]

    merged = SCAN_DIR / f"scan_{stamp}.pdf"
    if merge_pdfs([str(p) for p in pages], merged):
        for p in pages:
            p.unlink()
        log(f"Merged {len(pages)} page(s) into {merged.name}\n")
        return [merged.name]

    log(
        "Couldn't merge pages (pip install pypdf, or install poppler-utils). "
        "Keeping one PDF per page.\n"
    )
    return [p.name for p in pages]


def run_scan(device: str, resolution: str, mode: str, paper: str, output: str) -> None:
    """Scan every sheet in the feeder, then merge or keep the pages as requested.

    Runs in a background thread and reports through the shared `state` dict.
    """
    stamp = datetime.now().strftime("%F_%H%M%S")
    try:
        fmt = choose_format(output)
        SCAN_DIR.mkdir(parents=True, exist_ok=True)
        stream_command(build_command(device, resolution, mode, paper, fmt, stamp))

        pages = sorted(SCAN_DIR.glob(f"scan_{stamp}_*.{fmt}"))
        if not pages:
            raise RuntimeError(
                "No pages were scanned. Check that paper is loaded and the scanner is awake."
            )
        files = finalise(pages, output, fmt, stamp)
        with lock:
            state["files"] = files
    except Exception as exc:  # noqa: BLE001
        with lock:
            state["error"] = str(exc)
    finally:
        with lock:
            state["running"] = False


def run_page_scan(
    device: str, resolution: str, mode: str, paper: str, output: str
) -> None:
    """Scan one sheet and add it to the current session."""
    try:
        fmt = choose_format(output)  # calls log(), so must run outside the lock
        with lock:
            if session["stamp"] is None:
                session.update(
                    stamp=datetime.now().strftime("%F_%H%M%S"),
                    fmt=fmt,
                    output=output,
                    pages=[],
                )
            stamp, fmt = session["stamp"], session["fmt"]
            n = len(session["pages"]) + 1
        SCAN_DIR.mkdir(parents=True, exist_ok=True)
        dest = SCAN_DIR / f"scan_{stamp}_{n:03d}.{fmt}"

        cmd = [
            "scanimage",
            "-d",
            device,
            "--source",
            SOURCE,
            "--mode",
            mode,
            "--resolution",
            resolution,
            "--format",
            fmt,
        ]
        if PAPER_HEIGHT_MM[paper]:
            cmd += ["-y", str(PAPER_HEIGHT_MM[paper])]
        log("$ " + " ".join(cmd) + f" > {dest.name}\n")

        with open(dest, "wb") as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, check=False)
        log(proc.stderr.decode(errors="replace"))
        if proc.returncode != 0 or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise RuntimeError(
                "The page didn't scan. Check a sheet is loaded and the scanner is awake."
            )
        with lock:
            session["pages"].append(str(dest))
    except Exception as exc:  # noqa: BLE001
        with lock:
            state["error"] = str(exc)
    finally:
        with lock:
            state["running"] = False


@app.get("/")
def index():
    return render_template("index.html", default_device=DEFAULT_DEVICE)


@app.get("/api/devices")
def api_devices():
    return jsonify(list_devices())


def start_job(target):
    """Validate the request and run `target` in a background thread."""
    data = request.get_json(force=True, silent=True) or {}
    device = data.get("device", DEFAULT_DEVICE)
    resolution = str(data.get("resolution", "300"))
    mode = data.get("mode", "Color")
    paper = data.get("paper", "auto")
    output = data.get("output", "pdf")
    if device not in (known_devices or list_devices()):
        return jsonify(error="Unknown scanner. Refresh the device list."), 400
    if (
        resolution not in RESOLUTIONS
        or mode not in MODES
        or paper not in PAPER_HEIGHT_MM
        or output not in OUTPUTS
    ):
        return jsonify(error="Invalid option."), 400
    with lock:
        if state["running"]:
            return jsonify(error="A scan is already running."), 409
        if target is run_scan and session["pages"]:
            return jsonify(error="Finish or discard the page-by-page scan first."), 409
        state.update(running=True, log="", files=[], error=None)
    threading.Thread(
        target=target, args=(device, resolution, mode, paper, output), daemon=True
    ).start()
    return jsonify(ok=True)


@app.post("/api/scan")
def api_scan():
    return start_job(run_scan)


@app.post("/api/scan_page")
def api_scan_page():
    return start_job(run_page_scan)


@app.post("/api/finish")
def api_finish():
    with lock:
        if state["running"]:
            return jsonify(error="A scan is still running."), 409
        if not session["pages"]:
            return jsonify(error="No pages scanned yet."), 400
        pages = [Path(p) for p in session["pages"]]
        stamp, fmt, output = session["stamp"], session["fmt"], session["output"]
        session.update(stamp=None, fmt=None, output=None, pages=[])
    return jsonify(files=finalise(pages, output, fmt, stamp))


@app.post("/api/discard")
def api_discard():
    with lock:
        if state["running"]:
            return jsonify(error="A scan is still running."), 409
        for p in session["pages"]:
            Path(p).unlink(missing_ok=True)
        session.update(stamp=None, fmt=None, output=None, pages=[])
    return jsonify(ok=True)


@app.get("/api/status")
def api_status():
    with lock:
        return jsonify({**state, "pages": len(session["pages"])})


@app.get("/api/recent")
def api_recent():
    files = (
        sorted(SCAN_DIR.glob("scan_*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if SCAN_DIR.exists()
        else []
    )
    return jsonify([p.name for p in files[:12]])


@app.get("/files/<path:name>")
def files(name):
    if "/" in name or not (SCAN_DIR / name).is_file():
        abort(404)
    return send_from_directory(SCAN_DIR, name)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    app.run(host=args.host, port=args.port, threaded=True)
