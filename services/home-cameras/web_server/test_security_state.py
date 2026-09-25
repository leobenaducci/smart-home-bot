"""Presence from tracks, and what reaches Home Assistant.

Plain Python, like test_object_tracker.py: no cv2, no torch, no broker. The
fake client records what would have been published.
"""
import json
import unittest

from security_state import CameraPresence, SecurityPublisher


class FakeMqtt:
    topic_prefix = 'homecameras'

    def __init__(self):
        self.published = []      # (topic, payload, retain)

    def publish(self, suffix, payload, retain=False):
        self.published.append((f"{self.topic_prefix}/{suffix}", payload, retain))

    def publish_raw(self, topic, payload, retain=False):
        self.published.append((topic, json.loads(payload), retain))


def track(tid, cls='person', last_seen=0.0, stationary=False, moved=50.0):
    return {'track_id': tid, 'class_name': cls, 'hits': 3, 'first_seen': last_seen - 1,
            'confirmed_at': last_seen, 'last_seen': last_seen, 'moved_px': moved,
            'stationary': stationary, 'bbox': [0, 0, 40, 80]}


class TestCameraPresence(unittest.TestCase):
    def test_a_confirmed_person_is_present_and_leaves_after_the_hold(self):
        p = CameraPresence('patio', hold_seconds=30)
        events = p.update([track(1, last_seen=100.0)], 100.0)
        self.assertEqual([e['event'] for e in events], ['arrived'])
        self.assertTrue(p.present['person'])
        self.assertEqual(p.active['person'], 1)
        # nothing tracked for a while: still present inside the hold...
        self.assertEqual(p.update([], 120.0), [])
        self.assertTrue(p.present['person'])
        # ...and gone after it, once
        events = p.update([], 131.0)
        self.assertEqual([e['event'] for e in events], ['left'])
        self.assertFalse(p.present['person'])
        self.assertEqual(p.update([], 140.0), [])

    def test_a_stationary_object_is_present_but_not_active(self):
        p = CameraPresence('street', hold_seconds=30)
        p.update([track(7, cls='car', last_seen=10.0, stationary=True)], 10.0)
        self.assertTrue(p.present['vehicle'])
        self.assertEqual(p.active['vehicle'], 0)
        self.assertEqual(p.stationary['vehicle'], 1)

    def test_only_the_kinds_that_matter_become_presence(self):
        p = CameraPresence('patio')
        events = p.update([track(1, cls='bed', last_seen=5.0), track(2, cls='chair', last_seen=5.0)], 5.0)
        self.assertEqual(events, [])
        self.assertFalse(any(p.present.values()))

    def test_an_arrival_fires_once_per_track(self):
        p = CameraPresence('patio')
        self.assertEqual(len(p.update([track(1, last_seen=1.0)], 1.0)), 1)
        self.assertEqual(len(p.update([track(1, last_seen=1.2)], 1.2)), 0)
        self.assertEqual(len(p.update([track(1, last_seen=1.4), track(2, last_seen=1.4)], 1.4)), 1)

    def test_the_payload_reads_like_a_sensor(self):
        p = CameraPresence('patio', hold_seconds=30)
        p.update([track(1, last_seen=50.0)], 50.0)
        pl = p.payload(52.0)
        self.assertTrue(pl['any_present'])
        self.assertEqual(pl['person']['present'], True)
        self.assertEqual(pl['person']['seconds_since_seen'], 2.0)
        self.assertEqual(pl['person']['since'], 50.0)
        self.assertEqual(pl['vehicle']['present'], False)


class TestSecurityPublisher(unittest.TestCase):
    def test_state_goes_out_on_change_and_events_on_arrival_and_departure(self):
        mq = FakeMqtt()
        pub = SecurityPublisher(mq, hold_seconds=30, republish_seconds=1000)
        pub.observe('patio', [track(1, last_seen=10.0)], 10.0, camera_name='Patio')
        topics = [t for t, _, _ in mq.published]
        self.assertIn('homecameras/security/patio/state', topics)
        self.assertTrue(all(r for t, _, r in mq.published if t.endswith('/state')), "state is retained")
        self.assertIn('homecameras/security/patio/event', topics)
        n = len(mq.published)
        pub.observe('patio', [track(1, last_seen=10.2)], 10.2)      # nothing changed
        self.assertEqual(len(mq.published), n)
        pub.observe('patio', [], 41.0)                               # presence lapsed
        ev = [p for t, p, _ in mq.published if t.endswith('/event')]
        self.assertEqual([e['event'] for e in ev], ['arrived', 'left'])

    def test_home_assistant_discovery_is_retained_and_names_the_state_topic(self):
        mq = FakeMqtt()
        pub = SecurityPublisher(mq, hold_seconds=30)
        pub.observe('patio', [], 0.0, camera_name='Patio')
        disc = [(t, p, r) for t, p, r in mq.published if t.startswith('homeassistant/')]
        self.assertEqual(len(disc), 2)
        for topic, payload, retain in disc:
            self.assertTrue(retain)
            self.assertEqual(payload['state_topic'], 'homecameras/security/patio/state')
            self.assertEqual(payload['device_class'], 'occupancy')
            self.assertIn('expire_after', payload)
        self.assertEqual(disc[0][0], 'homeassistant/binary_sensor/homecameras_patio_person/config')
        self.assertEqual(disc[0][1]['name'], 'person')
        self.assertTrue(disc[0][1]['has_entity_name'])
        self.assertEqual(disc[0][1]['device']['name'], 'Camera Patio')

    def test_a_missing_broker_costs_nothing(self):
        pub = SecurityPublisher(lambda: None)
        self.assertEqual([e['event'] for e in pub.observe('patio', [track(1, last_seen=1.0)], 1.0)], ['arrived'])


if __name__ == '__main__':
    unittest.main()
