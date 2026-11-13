import unittest
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import slides2rm


class CourseTimezoneTests(unittest.TestCase):
    def test_day_uses_configured_course_timezone(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 1, 1, 1, tzinfo=timezone.utc).astimezone(tz)

        with patch.object(slides2rm, "datetime", FixedDatetime):
            self.assertEqual(slides2rm.today({"canvas": {"timezone": "America/Los_Angeles"}}), "2025-12-31")
            self.assertEqual(slides2rm.today({"canvas": {"timezone": "Asia/Tokyo"}}), "2026-01-01")
            self.assertEqual(slides2rm.today({}), "2026-01-01")


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / "lecture.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=800, height=450)
        page.insert_text((40, 45), "Lecture title", fontsize=22)
        page.insert_text((40, 110), "Slide one", fontsize=16)
        page = doc.new_page(width=450, height=800)
        page.insert_text((40, 45), "Slide two", fontsize=16)
        doc.save(self.src)
        doc.close()

    def test_notes_and_full_keep_pages_text_and_fit_aspect(self):
        with pymupdf.open(self.src) as src:
            source_ratio = src[0].rect.width / src[0].rect.height
        for mode, expected in (("notes", (slides2rm.PAGE_W, slides2rm.PAGE_H)),
                               ("full", (slides2rm.PAGE_H, slides2rm.PAGE_W))):
            dst = self.root / f"{mode}.pdf"
            self.assertEqual(slides2rm.layout(self.src, dst, mode, "lines"), 2)
            with pymupdf.open(dst) as out:
                self.assertEqual(len(out), 2)
                self.assertEqual((out[0].rect.width, out[0].rect.height), expected)
                self.assertIn("Slide one", out[0].get_text())
                self.assertIn("Slide two", out[1].get_text())
                slide_rect = pymupdf.Rect([x[3] for x in out[0].get_xobjects() if x[2] == 0][-1])
                self.assertAlmostEqual(slide_rect.width / slide_rect.height, source_ratio)
                if mode == "notes":
                    self.assertLessEqual(slide_rect.height, out[0].rect.height * .62)

    def test_circuit_pick_writes_caption_diagram_and_writing_space(self):
        src = pymupdf.open()
        page = src.new_page(width=400, height=300)
        page.insert_text((25, 30), "Circuit lecture", fontsize=18)
        page.draw_line((100, 130), (300, 130), width=2)
        page.draw_rect(pymupdf.Rect(170, 110, 210, 150), width=2)
        src_path = self.root / "circuit.pdf"
        src.save(src_path)
        src.close()
        picks = self.root / "picks.json"
        picks.write_text(json.dumps([{"slide": 1, "x0": .2, "y0": .3, "x1": .8, "y1": .6,
                                      "task": "Analyze the resistor circuit"}]))
        dst = self.root / "circuits.pdf"
        with patch.object(slides2rm, "pick_circuits", side_effect=AssertionError("Claude called")):
            self.assertEqual(slides2rm.layout(src_path, dst, "circuits", "dots", picks), 1)
        with pymupdf.open(dst) as out:
            self.assertEqual(len(out), 1)
            text = out[0].get_text()
            self.assertIn("Circuit lecture", text)
            self.assertIn("Analyze the resistor circuit", text)
            drawings = out[0].get_drawings()
            diagram = [d for d in drawings if d["color"] == (0, 0, 0)]
            self.assertGreaterEqual(len(diagram), 2)
            self.assertTrue(all(d["rect"].y1 < out[0].rect.height / 2 for d in diagram))
            self.assertTrue(any(d["fill"] is not None and d["fill"] != (1, 1, 1)
                                and d["rect"].y1 > out[0].rect.height / 2 for d in drawings))

    def test_ruled_creates_ruler_image_and_safe_name(self):
        with pymupdf.open(self.src) as doc:
            pix = slides2rm._ruled(doc[0], width=300)
            self.assertGreater(pix.width, 300)
            self.assertGreater(pix.height, 100)
            self.assertGreater(len(pix.samples), 1000)
        self.assertEqual(slides2rm.safe_name('a/b\\c:*?"<>|'), "a-b-c-")
        self.assertEqual(slides2rm.safe_name("///"), "-")
        self.assertEqual(slides2rm.safe_name(""), "slides")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "state.json"
        self.out_path = self.root / "out"
        self.state_patch = patch.object(slides2rm, "STATE", self.state_path)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.out_patch = patch.object(slides2rm, "OUT", self.out_path)
        self.out_patch.start()
        self.addCleanup(self.out_patch.stop)
        self.cfg = {"canvas": {"timezone": "UTC"}, "layout": {"mode": "notes", "grid": "none"},
                    "remarkable": {"folder": "Lectures"}}
        self.file = {"id": 17, "name": "Week 1.pdf", "where": "", "url": "fake",
                     "modified": "2026-11-12T12:00:00Z", "picked": True, "locked": False}

    def args(self, **kwargs):
        values = {"once_a_day": False, "latest": False, "baseline": False, "dry_run": False}
        values.update(kwargs)
        return SimpleNamespace(**values)

    def candidates(self, files=None):
        fake = SimpleNamespace(download=lambda url, dest: Path(dest).write_bytes(
            self._synthetic_pdf()))
        return patch.object(slides2rm, "candidates", return_value=(fake, [self.file] if files is None else files))

    def _synthetic_pdf(self):
        doc = pymupdf.open()
        doc.new_page().insert_text((40, 40), "Downloaded slide")
        data = doc.tobytes()
        doc.close()
        return data

    def test_empty_state_and_save_load(self):
        self.assertEqual(slides2rm.load_state(), {"files": {}})
        state = {"files": {"17": {"modified": "y"}}}
        slides2rm.save_state(state)
        self.assertEqual(slides2rm.load_state(), state)

    def test_modified_deck_uses_new_revision_preserving_old_revision(self):
        old = {"files": {"17": {"name": "Week 1.pdf", "modified": "old", "rev": 1}}}
        slides2rm.save_state(old)
        with self.candidates(), patch.object(slides2rm, "upload") as upload:
            self.assertEqual(slides2rm.cmd_sync(self.cfg, self.args()), 1)
        self.assertEqual(slides2rm.load_state()["files"]["17"]["rev"], 2)
        self.assertEqual(old["files"]["17"]["rev"], 1)
        self.assertTrue((self.out_path / "Week 1 (rev 2).pdf").exists())
        upload.assert_called_once()

    def test_once_a_day_returns_before_candidates(self):
        slides2rm.save_state({"files": {}, "last_delivery": slides2rm.today(self.cfg)})
        with patch.object(slides2rm, "candidates", side_effect=AssertionError("Canvas queried")):
            self.assertEqual(slides2rm.cmd_sync(self.cfg, self.args(once_a_day=True)), 0)

    def test_baseline_marks_existing_without_uploading(self):
        with self.candidates(), patch.object(slides2rm, "upload") as upload:
            self.assertIsNone(slides2rm.cmd_sync(self.cfg, self.args(baseline=True)))
        self.assertTrue(slides2rm.load_state()["files"]["17"]["baseline"])
        upload.assert_not_called()

    def test_dry_run_does_not_write_state_or_upload(self):
        with self.candidates(), patch.object(slides2rm, "upload") as upload:
            self.assertEqual(slides2rm.cmd_sync(self.cfg, self.args(dry_run=True)), 0)
        self.assertFalse(self.state_path.exists())
        upload.assert_not_called()

    def test_main_once_a_day_records_no_new_delivery(self):
        slides2rm.save_state({"files": {}, "last_delivery": slides2rm.today(self.cfg)})
        with patch.object(slides2rm, "CONFIG") as config, \
             patch.object(slides2rm.tomllib, "loads", return_value=self.cfg), \
             patch.object(slides2rm, "candidates", side_effect=AssertionError("Canvas queried")), \
             patch("sys.argv", ["slides2rm", "sync", "--once-a-day"]):
            config.read_text.return_value = ""
            with self.assertRaises(SystemExit) as exc:
                slides2rm.main()
        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(slides2rm.load_state()["last_delivery"], slides2rm.today(self.cfg))


if __name__ == "__main__":
    unittest.main()
