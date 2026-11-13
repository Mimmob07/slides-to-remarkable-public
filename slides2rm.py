#!/usr/bin/env python3
# Copyright (c) 2026 Mimmo Baluyut
# SPDX-License-Identifier: MIT
"""Fetch new lecture slides from Canvas, lay them out for a reMarkable, upload them.

    slides2rm.py list               show every candidate file and whether the filter picks it
    slides2rm.py sync               convert + upload anything not sent yet
    slides2rm.py sync --dry-run     show what sync would send
    slides2rm.py sync --baseline    mark everything currently posted as already sent
    slides2rm.py sync --latest      reprocess the newest available lecture deck
    slides2rm.py sync --once-a-day  do nothing, and leave Canvas alone, if new slides were already delivered today
    slides2rm.py fetch [N]          download the newest N decks untouched into out/, to check results against
    slides2rm.py convert IN [OUT]   lay out a local pdf/pptx without touching Canvas or the tablet

In the optional "circuits" layout Claude (the `claude` CLI) picks out the circuit diagrams and each
gets its own page with room to work; "notes" and "full" keep every slide.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pymupdf
import requests
from scipy import ndimage

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.toml"
STATE = ROOT / "state.json"
OUT = ROOT / "out"
TOKEN_FILE = Path.home() / ".config" / "slides2rm" / "canvas_token"
RMAPI = shutil.which("rmapi") or str(Path.home() / ".local/bin/rmapi")
SLIDE_EXTS = {".pdf", ".pptx", ".ppt"}

# Both the reMarkable 2 and the Paper Pro have 3:4 screens.
PAGE_W, PAGE_H = 540, 720
MARGIN = 10
GRID_STEP = 17  # ~5 mm once the page is scaled to the screen
GRID_COLOR = (0.62, 0.62, 0.62)

RENDER = 2  # pixels per point when looking for a diagram's edges


class Fatal(Exception):
    pass


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- layout

def _notes_template(slot_h, grid):
    """One-page PDF holding the writing-space background, reused on every page."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    top = MARGIN + slot_h + GRID_STEP
    shape = page.new_shape()
    if grid == "dots":
        y = top
        while y < PAGE_H - MARGIN:
            x = MARGIN + GRID_STEP / 2
            while x < PAGE_W - MARGIN:
                shape.draw_circle((x, y), 0.55)
                x += GRID_STEP
            y += GRID_STEP
        shape.finish(fill=GRID_COLOR, color=None)
    elif grid == "lines":
        y = top + GRID_STEP
        while y < PAGE_H - MARGIN:
            shape.draw_line((MARGIN, y), (PAGE_W - MARGIN, y))
            y += GRID_STEP * 1.5
        shape.finish(color=GRID_COLOR, width=0.4)
    shape.commit()
    return doc


PICK_PROMPT = """\
The files slide-001.png to slide-{n:03}.png in this directory are the slides of one university \
circuits lecture. Read every one of them, in order.

I follow the lecture on a tablet and want a page for every circuit diagram in it, to redraw, solve \
and take notes on. List every circuit schematic: a drawing made of circuit symbols (sources, \
resistors, capacitors, inductors, diodes, transistors, op amps, switches, wires). When unsure \
whether a schematic is worth having, include it.

- A slide often holds several schematics, for example a circuit and beside or below it the same \
circuit redrawn, simplified or with a part replaced. Each one is its own entry.
- Keep a schematic whatever else is on the slide. Equations, derivations, bullet text or plots next \
to it are no reason to leave it out.
- A single component drawn with its voltage and current reference marks (for example a capacitor \
symbol with + v - and an arrow for i) counts as a schematic.
- Not schematics: photos, plots and waveforms, tables, block diagrams, equations, and pictures of \
how a physical part is built (capacitor plates, a coil on a core), even when they carry v and i marks.
- When slides with the same title show the same schematic (animation builds, or a solution \
appearing step by step), list it once, from the slide where the schematic is complete and has the \
least written over or around it. When the same schematic comes back under a different title, list \
it again there.

Every image has a ruler along its top and left edges, numbered 0 to 100, and faint grid lines every \
10. For each schematic give the slide number and a box in ruler units: x0 and x1 read off the top \
ruler, y0 and y1 off the left ruler. The box must contain the whole schematic with all its component \
labels, values, polarity marks, current arrows and node or terminal names, and nothing else: no \
neighbouring equations, sentences or other schematics. Make it a little too large rather than cut \
anything off. Also give a task of at most 12 plain ASCII words saying what the slide does with the \
schematic (empty if it does not say)."""

PICK_SCHEMA = {"type": "object", "required": ["diagrams"], "properties": {"diagrams": {"type": "array", "items": {
    "type": "object", "required": ["slide", "x0", "y0", "x1", "y1", "task"], "properties": {
        "slide": {"type": "integer"}, "task": {"type": "string"},
        "x0": {"type": "number"}, "y0": {"type": "number"}, "x1": {"type": "number"}, "y1": {"type": "number"}}}}}}


def _ruled(page, width=1100, left=34, top=20):
    """PNG of a slide with a 0-100 ruler on its top and left edges, so boxes can be read off it."""
    k = width / page.rect.width
    w, h = width, page.rect.height * k
    doc = pymupdf.open()
    sheet = doc.new_page(width=left + w + 20, height=top + h + 6)
    sheet.show_pdf_page(pymupdf.Rect(left, top, left + w, top + h), page.parent, page.number)
    shape = sheet.new_shape()
    for i in range(1, 10):
        shape.draw_line((left + w * i / 10, top), (left + w * i / 10, top + h))
        shape.draw_line((left, top + h * i / 10), (left + w, top + h * i / 10))
    shape.finish(color=(0.8, 0.8, 0.8), width=0.5, dashes="[2 5] 0")
    shape.draw_rect(pymupdf.Rect(left, top, left + w, top + h))
    for i in range(21):
        tick = 7 if i % 2 == 0 else 4
        shape.draw_line((left + w * i / 20, top - tick), (left + w * i / 20, top))
        shape.draw_line((left - tick, top + h * i / 20), (left, top + h * i / 20))
    shape.finish(color=(0, 0, 0), width=0.7)
    shape.commit()
    for i in range(11):
        sheet.insert_text((left + w * i / 10 + 2, top - 8), str(i * 10), fontsize=9)
        sheet.insert_text((2, top + h * i / 10 + (9 if i == 0 else 3)), str(i * 10), fontsize=9)
    return sheet.get_pixmap()


def pick_circuits(src):
    """Ask Claude which parts of which slides are circuit diagrams, as fractions of the slide."""
    claude = shutil.which("claude")
    if not claude:
        raise Fatal("the `claude` CLI is needed to pick out circuit diagrams")
    with tempfile.TemporaryDirectory() as tmp:
        for i, page in enumerate(src, 1):
            _ruled(page).save(f"{tmp}/slide-{i:03}.png")
        r = subprocess.run([claude, "-p", PICK_PROMPT.format(n=len(src)), "--model", "sonnet", "--allowedTools", "Read",
                            "--output-format", "json", "--json-schema", json.dumps(PICK_SCHEMA)],
                           cwd=tmp, capture_output=True, text=True, timeout=1500, stdin=subprocess.DEVNULL)
    try:
        picks = json.loads(r.stdout)["structured_output"]["diagrams"]
    except (ValueError, KeyError, TypeError):
        raise Fatal(f"Claude could not pick the diagrams: {(r.stderr or r.stdout).strip()[-300:]}")
    picks = [{**d, **{e: d[e] / 100 for e in ("x0", "y0", "x1", "y1")}} for d in picks]
    return [d for d in picks if 1 <= d["slide"] <= len(src) and 0 <= d["x0"] < d["x1"] <= 1 and 0 <= d["y0"] < d["y1"] <= 1]


def _blobs(ink, rx, ry):
    """Number the groups of drawn pixels that lie within 2*rx by 2*ry pixels of each other."""
    labels, n = ndimage.label(ndimage.maximum_filter(ink, size=(2 * ry + 1, 2 * rx + 1)))
    labels[~ink] = 0
    return labels, n, np.bincount(labels.ravel(), minlength=n + 1)


def refine_box(page, seed):
    """Turn Claude's rough box into a clean crop: (clip, patches to paint over, background colour).

    The slide is rendered and split into blobs of ink. The crop is the box around the blobs Claude's
    box points at (the wires, plus the labels sitting in and around them), so it never cuts through
    part of the diagram, and whatever else pokes into that box is painted over rather than left as
    a sliced-off fragment. Working on pixels makes pasted images and vector drawings behave alike.
    """
    pr = page.rect
    pix = page.get_pixmap(matrix=pymupdf.Matrix(RENDER, RENDER), alpha=False)
    rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3).astype(np.int32)
    colors, counts = np.unique(rgb[::4, ::4].reshape(-1, 3), axis=0, return_counts=True)
    bg = colors[counts.argmax()]
    ink = np.abs(rgb - bg).max(axis=2) > 24
    H, W = ink.shape
    # Slide furniture (header rules, page borders) spans the slide; take it out first, so that a
    # wire ending right under the title rule is not swallowed by it. A diagram that itself spans
    # the slide keeps its long wires.
    rules = np.zeros_like(ink)
    if seed.width < 0.7 * pr.width:
        rules[(ink.sum(axis=1) > 0.8 * W), :] = True
    if seed.height < 0.7 * pr.height:
        rules[:, (ink.sum(axis=0) > 0.8 * H)] = True
    ink &= ~rules

    def px(r):  # page rect -> pixel slices
        x0, y0 = max(0, int((r.x0 - pr.x0) * RENDER)), max(0, int((r.y0 - pr.y0) * RENDER))
        x1, y1 = min(W, int(np.ceil((r.x1 - pr.x0) * RENDER))), min(H, int(np.ceil((r.y1 - pr.y0) * RENDER)))
        return slice(y0, y1), slice(x0, x1)

    def pt(sl):  # pixel slices -> page rect
        return pymupdf.Rect(sl[1].start / RENDER + pr.x0, sl[0].start / RENDER + pr.y0,
                            sl[1].stop / RENDER + pr.x0, sl[0].stop / RENDER + pr.y0)

    inside = np.zeros_like(ink)
    inside[px(seed)] = True
    gap = round(2.5 * RENDER)
    labels, n, total = _blobs(ink, gap, gap)
    within = np.bincount(labels[inside], minlength=n + 1)
    boxes = [None] + [pt(sl) for sl in ndimage.find_objects(labels)]

    # The diagram's body: big blobs lying mostly in Claude's box, or filling most of it.
    # Anything as large as the slide itself is background and left out of everything.
    furniture = {i for i in range(1, n + 1) if boxes[i].width > 0.8 * pr.width or abs(boxes[i]) > 0.5 * abs(pr)}
    big = {i for i in range(1, n + 1) if max(boxes[i].width, boxes[i].height) >= 0.08 * pr.width} - furniture
    body = {i for i in big if within[i] >= 0.5 * total[i] or abs(boxes[i] & seed) >= 0.5 * abs(seed)}

    # The rest is labels and text. Grouped into runs (text reads left to right, so the grouping
    # reaches further sideways), a run is kept if most of it is in Claude's box: that keeps
    # "20 kΩ" and drops the end of a sentence or equation that happens to reach into the box.
    rest = ink & ~np.isin(labels, list(body | furniture))
    runs, m, run_total = _blobs(rest, round(6 * RENDER), gap)
    run_within = np.bincount(runs[inside], minlength=m + 1)
    run_boxes = [None] + [pt(sl) for sl in ndimage.find_objects(runs)]
    kept = {i for i in range(1, m + 1) if run_within[i] >= 0.5 * run_total[i]}
    if not body and not kept:
        return (seed + (-0.02 * pr.width, -0.02 * pr.height, 0.02 * pr.width, 0.02 * pr.height)) & pr, [], bg / 255

    def union():
        box = pymupdf.Rect()
        for b in [boxes[i] for i in body] + [run_boxes[i] for i in kept]:
            box |= b
        return box

    # Short labels just outside the box (a "-" under a "+ v", a value beside a resistor) belong too.
    for _ in range(2):
        near = union() + (-8, -8, 8, 8)
        kept |= {i for i in range(1, m + 1) if run_boxes[i].width < 0.1 * pr.width
                 and run_boxes[i].height < 0.06 * pr.height and near.contains(run_boxes[i])}

    clip = (union() + (-5, -5, 5, 5)) & pr
    keep = np.isin(labels, list(body)) | np.isin(runs, list(kept))
    patches = []
    dropped = [run_boxes[i] for i in range(1, m + 1) if i not in kept]
    dropped += [pt(sl) for sl in ndimage.find_objects(ndimage.label(rules)[0])]
    for b in dropped:
        # Reaching just past the crop's edge keeps a hairline of the dropped ink from showing there.
        patch = (b + (-1.5, -1.5, 1.5, 1.5)) & (clip + (-1, -1, 1, 1))
        # Only paint where nothing of the diagram would be lost.
        if not patch.is_empty and not keep[px(patch)].any():
            patches.append(patch)
    return clip, patches, bg / 255


def slide_title(page):
    """The slide's heading: the largest text in its top quarter, ASCII only."""
    lines = [(max(span["size"] for span in line["spans"]), "".join(span["text"] for span in line["spans"]))
             for block in page.get_text("dict", clip=page.rect + (0, 0, 0, -0.75 * page.rect.height))["blocks"]
             for line in block.get("lines", []) if line["spans"]]
    lines = [(size, text.encode("ascii", "ignore").decode().strip()) for size, text in lines]
    lines = [(size, text) for size, text in lines if len(text) > 3]
    return max(lines)[1] if lines else ""


def layout_circuits(src, picks, dst_path, grid):
    """One page per picked diagram: the diagram cropped out at the top, the rest free to write on."""
    out = pymupdf.open()
    template = _notes_template(0, grid) if grid != "none" else None
    full_w = PAGE_W - 2 * MARGIN
    done = []
    for d in sorted(picks, key=lambda d: (d["slide"], d["y0"], d["x0"])):
        slide = src[d["slide"] - 1]
        r = slide.rect
        clip, patches, bg = refine_box(slide, pymupdf.Rect(r.x0 + d["x0"] * r.width, r.y0 + d["y0"] * r.height,
                                                           r.x0 + d["x1"] * r.width, r.y0 + d["y1"] * r.height))
        # Two picks that snapped to the same drawing are one diagram.
        if any(n == d["slide"] and abs(c & clip) > 0.7 * abs(c | clip) for n, c in done):
            continue
        done.append((d["slide"], clip))
        # Enlarge small diagrams, but no more than twice their size on a page-wide slide.
        k = min(full_w / clip.width, PAGE_H * 0.45 / clip.height, 2 * full_w / r.width)
        x0, y0 = (PAGE_W - clip.width * k) / 2, MARGIN + 16
        target = pymupdf.Rect(x0, y0, x0 + clip.width * k, y0 + clip.height * k)
        page = out.new_page(width=PAGE_W, height=PAGE_H)
        if template:
            page.show_pdf_page(page.rect, template, 0)
        page.draw_rect(pymupdf.Rect(0, 0, PAGE_W, target.y1 + 6), fill=(1, 1, 1), color=None)
        caption = "  -  ".join(t for t in (f"Slide {d['slide']}", slide_title(slide), d["task"].strip()) if t)
        page.insert_text((MARGIN, MARGIN + 9), caption.encode("ascii", "ignore").decode(), fontsize=9, color=(0.35, 0.35, 0.35))
        # The crop stays vector, so the diagram remains sharp when zoomed on the tablet.
        page.show_pdf_page(target, src, d["slide"] - 1, clip=clip)
        for patch in patches:
            page.draw_rect(pymupdf.Rect(x0 + (patch.x0 - clip.x0) * k, y0 + (patch.y0 - clip.y0) * k,
                                        x0 + (patch.x1 - clip.x0) * k, y0 + (patch.y1 - clip.y0) * k),
                           fill=tuple(bg), color=None)
    if len(out):
        out.set_metadata({"title": Path(dst_path).stem})
        out.save(dst_path, garbage=3, deflate=True)
    return len(out)


def layout(src_path, dst_path, mode="circuits", grid="dots", picks_file=None):
    """Write the tablet PDF and return its page count (0 = nothing worth sending)."""
    src = pymupdf.open(src_path)
    if src.needs_pass:
        raise Fatal(f"{src_path} is password protected")
    if mode == "circuits":
        # Claude's picks can be kept in a file, to rework the cropping without asking again.
        if picks_file and Path(picks_file).exists():
            picks = json.loads(Path(picks_file).read_text())
        else:
            picks = pick_circuits(src)
            if picks_file:
                Path(picks_file).write_text(json.dumps(picks, indent=1))
        return layout_circuits(src, picks, dst_path, grid)
    out = pymupdf.open()
    first = src[0].rect
    if mode == "full":
        page_w, page_h = PAGE_H, PAGE_W
        slot = pymupdf.Rect(0, 0, page_w, page_h)
        template = None
    else:
        page_w, page_h = PAGE_W, PAGE_H
        slot_w = page_w - 2 * MARGIN
        slot_h = min(slot_w * first.height / first.width, page_h * 0.62)
        slot = pymupdf.Rect(MARGIN, MARGIN, MARGIN + slot_w, MARGIN + slot_h)
        template = _notes_template(slot_h, grid) if grid != "none" else None

    for i in range(len(src)):
        page = out.new_page(width=page_w, height=page_h)
        if template:
            page.show_pdf_page(page.rect, template, 0)
        r = src[i].rect
        k = min(slot.width / r.width, slot.height / r.height)
        x0 = slot.x0 + (slot.width - r.width * k) / 2
        y0 = slot.y0 if mode != "full" else slot.y0 + (slot.height - r.height * k) / 2
        target = pymupdf.Rect(x0, y0, x0 + r.width * k, y0 + r.height * k)
        # Slides stay vector, so diagrams remain sharp when zoomed on the tablet.
        page.show_pdf_page(target, src, i, rotate=-src[i].rotation)
        if mode != "full":
            page.draw_rect(target, color=GRID_COLOR, width=0.5)
    out.set_metadata({"title": Path(dst_path).stem})
    out.save(dst_path, garbage=3, deflate=True)
    return len(src)


def to_pdf(path):
    """Return a PDF for `path`, converting PowerPoint files with LibreOffice."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        return path
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise Fatal(f"{path.name} is a PowerPoint file and LibreOffice is not installed "
                    "(sudo pacman -S libreoffice-fresh)")
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(path.parent), str(path)],
                   check=True, capture_output=True, timeout=300)
    pdf = path.with_suffix(".pdf")
    if not pdf.exists():
        raise Fatal(f"LibreOffice produced no PDF for {path.name}")
    return pdf


# ---------------------------------------------------------------- canvas

class Canvas:
    def __init__(self, base_url, modules=False):
        self.modules = modules
        self.requests = 0  # API calls made, logged so the footprint on Canvas stays visible
        token = os.environ.get("CANVAS_TOKEN") or (TOKEN_FILE.read_text() if TOKEN_FILE.exists() else "")
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.refresh_token = os.environ.get("CANVAS_REFRESH_TOKEN", "").strip()
        self.client_id = os.environ.get("CANVAS_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("CANVAS_CLIENT_SECRET", "").strip()
        if self.refresh_token:
            if not self.client_id or not self.client_secret:
                raise Fatal("Canvas refresh credentials are incomplete")
            self.refresh_access_token()
        elif token.strip():
            self.s.headers["Authorization"] = "Bearer " + token.strip()
        else:
            raise Fatal(f"no Canvas credentials (set CANVAS_REFRESH_TOKEN or CANVAS_TOKEN, or create {TOKEN_FILE})")

    def refresh_access_token(self):
        """Get a short-lived access token using Canvas's reusable refresh token."""
        try:
            self.requests += 1
            r = self.s.post(f"{self.base}/login/oauth2/token", data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            }, timeout=30)
            r.raise_for_status()
            token = r.json()["access_token"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise Fatal("Canvas could not refresh the mobile session") from exc
        self.s.headers["Authorization"] = "Bearer " + token

    def get(self, path, **params):
        """GET an API path, following pagination. Returns None if the resource is off limits."""
        url = path if path.startswith("http") else f"{self.base}/api/v1{path}"
        params.setdefault("per_page", 100)
        items = None
        while url:
            self.requests += 1
            r = self.s.get(url, params=params, timeout=30)
            if r.status_code == 401 and "WWW-Authenticate" in r.headers and self.refresh_token:
                self.refresh_access_token()
                self.requests += 1
                r = self.s.get(url, params=params, timeout=30)
            # Canvas answers 401 both for a bad token and for a forbidden resource;
            # only the bad-token case carries WWW-Authenticate.
            if r.status_code == 401 and "WWW-Authenticate" in r.headers:
                raise Fatal("Canvas rejected the token (expired or revoked?)")
            if r.status_code in (401, 403, 404):
                return None
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return data
            items = (items or []) + data
            url, params = r.links.get("next", {}).get("url"), None
        return items or []

    def courses(self, match):
        courses = self.get("/courses", enrollment_state="active")
        if courses is None:
            raise Fatal("Canvas would not list your courses")
        m = match.lower()
        hits = [c for c in courses if m in (c.get("name") or "").lower() or m in (c.get("course_code") or "").lower()]
        if not hits:
            raise Fatal(f"no active Canvas course matches {match!r}")
        return hits

    def slide_files(self, course):
        """Every pdf/pptx visible in the course's Files (and Modules, if asked for), as {file_id: info}."""
        cid, found = course["id"], {}

        def add(f, where):
            if not f or Path(f.get("display_name") or "").suffix.lower() not in SLIDE_EXTS:
                return
            found.setdefault(f["id"], {
                "id": f["id"], "name": f["display_name"], "where": where, "course": course.get("course_code") or course["name"],
                "url": f.get("url"), "locked": bool(f.get("locked_for_user")) or not f.get("url"),
                "modified": f.get("modified_at") or f.get("updated_at"), "size": f.get("size"),
            })

        folders = {f["id"]: f["full_name"].removeprefix("course files").strip("/")
                   for f in self.get(f"/courses/{cid}/folders") or []}
        for f in self.get(f"/courses/{cid}/files") or []:
            add(f, folders.get(f.get("folder_id"), ""))
        # Modules cost a request each, so they are only read when the config asks for them.
        for mod in (self.get(f"/courses/{cid}/modules") or []) if self.modules else []:
            for item in self.get(f"/courses/{cid}/modules/{mod['id']}/items") or []:
                if item.get("type") == "File" and item.get("content_id") not in found:
                    add(self.get(item["url"]), mod["name"])
        return found

    def download(self, url, dest):
        with self.s.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1 << 16):
                    fh.write(chunk)


def candidates(cfg):
    c = cfg["canvas"]
    canvas = Canvas(c["base_url"], c.get("modules", False))
    include = re.compile(c.get("include") or ".")
    exclude = re.compile(c["exclude"]) if c.get("exclude") else None
    files = []
    for course in canvas.courses(c["course_match"]):
        for f in canvas.slide_files(course).values():
            label = f"{f['where']}/{f['name']}"
            f["picked"] = bool(include.search(label)) and not (exclude and exclude.search(label))
            files.append(f)
    files.sort(key=lambda f: f["modified"] or "")
    log(f"Canvas API requests: {canvas.requests} (plus one download per deck sent)")
    return canvas, files


# ---------------------------------------------------------------- remarkable

def rmapi(*args):
    r = subprocess.run([RMAPI, "-ni", *args], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        out = (r.stderr or r.stdout).strip()
        if "missing token" in out or "Unauthorized" in out:
            raise Fatal("reMarkable cloud is not paired (run `rmapi` in a terminal once)")
        raise Fatal(f"rmapi {args[0]} failed: {(out.splitlines() or ['unknown error'])[-1]}")
    return r.stdout


def upload(pdf, folder):
    folder = "/" + folder.strip("/")
    try:
        rmapi("ls", folder)
    except Fatal:
        rmapi("mkdir", folder)
    try:
        rmapi("put", str(pdf), folder)
    except Fatal as e:
        # A run that uploaded but died before recording it must not get stuck retrying.
        if "already exists" not in str(e):
            raise


# ---------------------------------------------------------------- commands

def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {"files": {}}


def save_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)


def safe_name(name):
    return re.sub(r'[/\\:*?"<>|]+', "-", name).strip() or "slides"


def cmd_list(cfg, args):
    _, files = candidates(cfg)
    sent = load_state()["files"]
    for f in files:
        mark = "sent  " if str(f["id"]) in sent else "SEND  " if f["picked"] else "skip  "
        lock = " [locked]" if f["locked"] else ""
        log(f"{mark}{(f['modified'] or '')[:10]}  {f['where']}/{f['name']}{lock}")
    log(f"\n{len(files)} files, {sum(f['picked'] for f in files)} match the filter")


def today(cfg):
    return datetime.now(ZoneInfo(cfg.get("canvas", {}).get("timezone", "UTC"))).date().isoformat()


def cmd_sync(cfg, args):
    """Returns how many new decks were dealt with."""
    # Once a run has delivered the day's slides, later runs stop here, before anything is asked
    # of Canvas. A run that found nothing new does not count, so the next one looks again.
    if args.once_a_day and load_state().get("last_delivery") == today(cfg):
        log("today's slides were already delivered, leaving Canvas alone")
        return 0
    canvas, files = candidates(cfg)
    state = load_state()
    sent = state["files"]
    # Locked files are left alone until the instructor releases them.
    todo = [f for f in files if f["picked"] and not f["locked"]
            and sent.get(str(f["id"]), {}).get("modified") != f["modified"]]

    if args.latest:
        available = [f for f in files if f["picked"] and not f["locked"]]
        if not available:
            raise Fatal("no available lecture deck matches the filter")
        todo = [max(available, key=lambda f: f["modified"] or "")]

    if args.baseline:
        for f in todo:
            sent[str(f["id"])] = {"name": f["name"], "modified": f["modified"], "rev": 1, "baseline": True}
        save_state(state)
        log(f"marked {len(todo)} files as already sent")
        return
    if not todo:
        log("nothing new")
        return 0

    OUT.mkdir(exist_ok=True)
    layout_cfg = cfg.get("layout", {})
    failed = []
    for f in todo:
        prev = sent.get(str(f["id"]))
        rev = prev["rev"] + 1 if prev else 1
        # A re-posted deck goes up as a new document so notes on the old one survive.
        title = safe_name(Path(f["name"]).stem) + (f" (rev {rev})" if rev > 1 else "")
        log(f"{'would send' if args.dry_run else 'sending'}: {title}")
        if args.dry_run:
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                raw = Path(tmp) / ("src" + Path(f["name"]).suffix.lower())
                canvas.download(f["url"], raw)
                pdf = OUT / f"{title}.pdf"
                pages = layout(to_pdf(raw), pdf, layout_cfg.get("mode", "circuits"), layout_cfg.get("grid", "dots"))
            if pages:
                upload(pdf, cfg["remarkable"]["folder"])
            else:
                log("  no circuit diagrams in this deck, nothing to send")
        except (Fatal, subprocess.SubprocessError, pymupdf.FileDataError) as e:
            log(f"  failed: {e}")
            failed.append(f"{title}: {e}")
            continue
        sent[str(f["id"])] = {"name": f["name"], "modified": f["modified"], "rev": rev,
                              "pages": pages, "sent_at": datetime.now().isoformat(timespec="seconds")}
        save_state(state)
    if failed:
        raise Fatal("; ".join(failed))
    return 0 if args.dry_run else len(todo)


def cmd_fetch(cfg, args):
    canvas, files = candidates(cfg)
    OUT.mkdir(exist_ok=True)
    for f in [f for f in files if f["picked"] and not f["locked"]][-args.count:]:
        canvas.download(f["url"], OUT / f"source - {safe_name(f['name'])}")
        log(f"fetched: {f['name']}")


def cmd_convert(cfg, args):
    src = Path(args.src)
    dst = Path(args.dst) if args.dst else src.with_name(src.stem + " (rM).pdf")
    layout_cfg = cfg.get("layout", {})
    pages = layout(to_pdf(src), dst, args.mode or layout_cfg.get("mode", "circuits"), layout_cfg.get("grid", "dots"), args.picks)
    log(f"{dst} ({pages} pages)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("sync")
    s.add_argument("--dry-run", action="store_true")
    only_one = s.add_mutually_exclusive_group()
    only_one.add_argument("--baseline", action="store_true")
    only_one.add_argument("--latest", action="store_true", help="reprocess the newest available lecture deck")
    s.add_argument("--once-a-day", action="store_true", help="skip if new slides were already delivered today")
    sub.add_parser("fetch").add_argument("count", nargs="?", type=int, default=1)
    c = sub.add_parser("convert")
    c.add_argument("src")
    c.add_argument("dst", nargs="?")
    c.add_argument("--mode", choices=["circuits", "notes", "full"])
    c.add_argument("--picks", help="JSON file to keep Claude's picks in; reused when it exists")
    args = p.parse_args()
    cfg = tomllib.loads(CONFIG.read_text())

    error = delivered = None
    try:
        delivered = {"list": cmd_list, "sync": cmd_sync, "fetch": cmd_fetch, "convert": cmd_convert}[args.cmd](cfg, args)
    except (requests.ConnectionError, requests.Timeout) as e:
        log(f"error: {e}")
        error = "network error reaching Canvas"
    except (Fatal, requests.RequestException, subprocess.SubprocessError, OSError) as e:
        error = str(e)
        log(f"error: {error}")
    if args.cmd == "sync" and not args.dry_run:
        # The scheduler mails on every failed run, so a failure that just repeats
        # the previous one is logged but not reported again.
        state = load_state()
        if delivered and not error and not args.baseline and not args.latest:
            state["last_delivery"] = today(cfg)
        if state.get("last_error") == error:
            error = None
        else:
            state["last_error"] = error
        save_state(state)
    sys.exit(1 if error else 0)


if __name__ == "__main__":
    main()
