"""What the reviewer is told about the clip before it looks at it.

Run: python3 web_server/test_review_prompt.py

The vision model used to get three stills and a generic instruction, and it
answered the way anything does when asked to find meaning in noise: "a small
object moved on the ground", "shadow of tree moved across patio". Neither is an
event. Both were the model describing scenery because nothing told it what the
clip was supposed to be about.

The recorder's detector already knows -- it is why the clip exists at all now --
so it says so. What is pinned here is that the two halves cannot be confused
with each other, and that the answer template survives being interpolated.
"""

import unittest

import clip_review as C


class TestDetectorTags(unittest.TestCase):
    def test_it_reads_the_tags_off_the_filename(self):
        self.assertEqual(
            C.detector_tags_in('2026-09-03_10-00-00_Front_person_car.mp4'),
            ['person', 'car'])

    def test_vehicles_count_here_even_though_they_cannot_veto_a_deletion(self):
        self.assertIn('car', C.DETECTOR_TAGS)
        self.assertNotIn('car', C.LIVING_TAGS)

    def test_an_untagged_clip_names_nothing(self):
        self.assertEqual(C.detector_tags_in('2026-09-03_10-00-00_Front.mp4'), [])

    def test_a_camera_name_is_never_mistaken_for_a_tag(self):
        self.assertEqual(C.detector_tags_in('2026-09-03_10-00-00_person.mp4'), [])

    def test_a_repeated_tag_is_named_once(self):
        self.assertEqual(
            C.detector_tags_in('2026-09-03_10-00-00_Front_car_car.mp4'), ['car'])


class TestPrompt(unittest.TestCase):
    def test_the_answer_template_survives(self):
        """The prompt carries literal JSON braces; interpolating must not eat them."""
        for name in ('2026-09-03_10-00-00_Front_person.mp4',
                     '2026-09-03_10-00-00_Front.mp4'):
            out = C.prompt_for(name)
            self.assertIn('{"keep": true|false', out)
            self.assertNotIn('{detector}', out)
            self.assertNotIn('{tags}', out)

    def test_a_tagged_clip_is_told_what_was_found(self):
        out = C.prompt_for('2026-09-03_10-00-00_Front_person_dog.mp4')
        self.assertIn('person, dog', out)
        self.assertIn('LOOK AT THOSE', out)
        self.assertNotIn('found no person', out)

    def test_an_untagged_clip_is_not_told_something_was_found(self):
        out = C.prompt_for('2026-09-03_10-00-00_Front.mp4')
        self.assertIn('found no person', out)
        self.assertNotIn('LOOK AT THOSE', out)

    def test_both_halves_name_the_scenery_to_ignore(self):
        """The two the house asked for by name, plus the usual suspects."""
        for name in ('2026-09-03_10-00-00_Front_person.mp4',
                     '2026-09-03_10-00-00_Front.mp4'):
            out = C.prompt_for(name).lower()
            for scenery in ('plants', 'date and time', 'shadows', 'insects',
                            'rain', 'leaves'):
                self.assertIn(scenery, out, f'{scenery} missing from {name}')

    def test_a_missing_filename_still_produces_a_usable_prompt(self):
        """_ask_model defaults it, and a broken prompt would fail every clip."""
        out = C.prompt_for('')
        self.assertIn('{"keep": true|false', out)
        self.assertIn('found no person', out)


class TestItReachesTheModel(unittest.TestCase):
    """A prompt built from the filename is no use if the filename is dropped."""

    def _capture(self):
        seen = {}
        real = C._ask_model

        def spy(frames, filename=''):
            seen['filename'] = filename
            return {'keep': True, 'what': 'x', 'certainty': 'high',
                    'kind': 'person', 'error': None}
        C._ask_model = spy
        self.addCleanup(lambda: setattr(C, '_ask_model', real))
        return seen

    def test_review_clip_passes_the_filename(self):
        seen = self._capture()
        real_frames = C._extract_frames
        C._extract_frames = lambda path, count: [b'frame']
        self.addCleanup(lambda: setattr(C, '_extract_frames', real_frames))
        C.review_clip('/tmp/2026-09-03_10-00-00_Front_person.mp4')
        self.assertEqual(seen['filename'], '2026-09-03_10-00-00_Front_person.mp4')


if __name__ == '__main__':
    unittest.main(verbosity=2)
