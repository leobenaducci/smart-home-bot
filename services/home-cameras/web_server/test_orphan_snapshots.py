"""Snapshots outliving the recording they belong to.

Run: python3 web_server/test_orphan_snapshots.py

Deleting a video left its snapshots behind, and there are three or four per
clip. When the household emptied the month folders on 2026-09-03 the videos
came to nothing and 31,451 JPEGs and 29 GB stayed -- as much disk as the video
had been.

This deletes files, so what is pinned here is mostly the refusals: what it must
NOT touch. A snapshot wrongly deleted is not recoverable, and the sweep runs
every 60 seconds unattended.
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

# Before importing: the module's default RECORDINGS_DIR is derived from its own
# location, and a suite that swept the real tree is the accident this house has
# already had.
_ROOT = tempfile.mkdtemp(prefix='cams-orphan-')
os.environ['RECORDINGS_DIR'] = _ROOT
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import recording as R  # noqa: E402

BASE = datetime(2026, 9, 3, 21, 20, 0)


def name(offset_s, camera='Front', ext='.jpg', tags=''):
    when = (BASE + timedelta(seconds=offset_s)).strftime('%Y-%m-%d_%H-%M-%S')
    return f'{when}_{camera}{tags}{ext}'


class OrphanCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='cams-orphan-case-')
        self.month = os.path.join(self.root, '2026-09')
        os.makedirs(self.month)
        self._real = R.RECORDINGS_DIR
        R.RECORDINGS_DIR = self.root
        self.addCleanup(lambda: setattr(R, 'RECORDINGS_DIR', self._real))
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, filename, where=None):
        path = os.path.join(where or self.month, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(b'x')
        return path

    def survivors(self):
        return sorted(os.listdir(self.month))

    def sweep(self, **kw):
        return R.purge_orphan_snapshots(self.root, **kw)


class TestWhatItDeletes(OrphanCase):
    def test_a_snapshot_with_no_clip_at_all_goes(self):
        self.write(name(0))
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.survivors(), [])

    def test_the_whole_backlog_goes_when_every_video_is_gone(self):
        for i in range(20):
            self.write(name(i * 30))
        self.assertEqual(self.sweep(), 20)
        self.assertEqual(self.survivors(), [])

    def test_a_snapshot_from_before_its_camera_started_recording_goes(self):
        """A clip cannot own a snapshot taken before it began."""
        self.write(name(0))
        self.write(name(60, ext='.mp4'))
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.survivors(), [name(60, ext='.mp4')])

    def test_a_snapshot_past_the_window_goes(self):
        self.write(name(0, ext='.mp4'))
        self.write(name(400))
        self.assertEqual(self.sweep(window_s=300), 1)


class TestWhatItRefusesToDelete(OrphanCase):
    """The half that matters: a snapshot deleted in error does not come back."""

    def test_a_snapshot_inside_its_clip_stays(self):
        self.write(name(0, ext='.mp4'))
        self.write(name(6))
        self.assertEqual(self.sweep(), 0)

    def test_the_snapshot_sharing_its_clips_name_stays(self):
        self.write(name(0, ext='.mp4'))
        self.write(name(0))
        self.assertEqual(self.sweep(), 0)

    def test_another_cameras_clip_does_not_save_it(self):
        self.write(name(0, camera='Patio', ext='.mp4'))
        self.write(name(6, camera='Front'))
        self.assertEqual(self.sweep(), 1)

    def test_a_cameras_own_clip_does_save_it(self):
        self.write(name(0, camera='Patio', ext='.mp4'))
        self.write(name(6, camera='Patio'))
        self.assertEqual(self.sweep(), 0)

    def test_tags_do_not_break_the_pairing(self):
        self.write(name(0, ext='.mp4', tags='_person_car'))
        self.write(name(6, tags='_cat'))
        self.assertEqual(self.sweep(), 0)

    def test_it_never_touches_a_video(self):
        self.write(name(0, ext='.mp4'))
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.survivors(), [name(0, ext='.mp4')])

    def test_it_never_touches_pending_or_the_review_tray(self):
        for folder in ('pending', 'to_review'):
            self.write(name(0), where=os.path.join(self.root, folder))
        self.assertEqual(self.sweep(), 0)
        for folder in ('pending', 'to_review'):
            self.assertEqual(os.listdir(os.path.join(self.root, folder)),
                             [name(0)])

    def test_a_clip_still_in_pending_keeps_its_snapshots(self):
        """The clip being recorded right now lives in pending/, and its
        snapshots are already in the month folder. Sweeping on month folders
        alone deleted them 60 seconds after they were taken."""
        self.write(name(0, ext='.mp4'), where=os.path.join(self.root, 'pending'))
        self.write(name(6))
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.survivors(), [name(6)])

    def test_a_clip_waiting_in_the_review_tray_keeps_them_too(self):
        self.write(name(0, ext='.mp4'),
                   where=os.path.join(self.root, 'to_review'))
        self.write(name(6))
        self.assertEqual(self.sweep(), 0)

    def test_but_a_staged_clip_for_another_camera_does_not_save_it(self):
        self.write(name(0, camera='Patio', ext='.mp4'),
                   where=os.path.join(self.root, 'pending'))
        self.write(name(6, camera='Front'))
        self.assertEqual(self.sweep(), 1)

    def test_it_never_touches_a_directory_it_does_not_recognise(self):
        self.write(name(0), where=os.path.join(self.root, 'keep-these'))
        self.assertEqual(self.sweep(), 0)

    def test_a_name_it_cannot_read_is_left_alone(self):
        for odd in ('holiday.jpg', 'IMG_1234.jpg', '2026-13-45_99-99-99_X.jpg'):
            self.write(odd)
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(len(self.survivors()), 3)

    def test_a_window_of_zero_disables_it_entirely(self):
        self.write(name(0))
        self.assertEqual(self.sweep(window_s=0), 0)
        self.assertEqual(len(self.survivors()), 1)

    def test_a_missing_tree_is_not_an_error(self):
        self.assertEqual(R.purge_orphan_snapshots('/nonexistent/anywhere'), 0)


class TestDeletingAClipTakesItsSnapshot(OrphanCase):
    def test_the_paired_snapshot_goes_with_the_clip(self):
        self.write(name(0, ext='.mp4'))
        self.write(name(0))
        self.write(name(6))
        self.assertTrue(R.delete_recording(f'2026-09/{name(0, ext=".mp4")}',
                                           'video'))
        self.assertEqual(self.survivors(), [name(6)])

    def test_deleting_a_snapshot_does_not_reach_for_a_video(self):
        self.write(name(0, ext='.mp4'))
        self.write(name(0))
        self.assertTrue(R.delete_recording(f'2026-09/{name(0)}', 'snapshot'))
        self.assertEqual(self.survivors(), [name(0, ext='.mp4')])

    def test_and_the_leftovers_are_swept_afterwards(self):
        """The two halves together: delete the clip, then the sweep tidies."""
        self.write(name(0, ext='.mp4'))
        self.write(name(0))
        self.write(name(6))
        R.delete_recording(f'2026-09/{name(0, ext=".mp4")}', 'video')
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.survivors(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
