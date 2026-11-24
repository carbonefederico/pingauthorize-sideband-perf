"""Offline regression checks for the three replacement files. No cluster required."""
import contextlib
import gzip
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('report', ROOT / 'scripts/generate-report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        report.RESULTS = Path(self.tmp.name) / 'results'
        report.REPORTS = Path(self.tmp.name) / 'reports'
        self.run = report.RESULTS / 'run-1'
        self.run.mkdir(parents=True)
        self.meta = {'RATE': '200', 'AGENTS': '2', 'DURATION': '1s', 'WARMUP': '30s',
                     'artifact_schema_version': 2, 'timestamp_utc': '2026-11-17T10:00:00Z',
                     'job_terminal_state': 'complete', 'agent_exit_codes': {'agent-0': 0, 'agent-1': 0}}
        self.events = {}
        for agent in range(2):
            events = []
            for scenario in ['mix_read', 'mix_session', 'mix_hitl', 'mix_deny']:
                for i in range(25):
                    for metric, value in [('http_req_duration', 5), ('sideband_decision_success', 1),
                                           ('sideband_request_elapsed', 7)]:
                        events.append({'type': 'Point', 'metric': metric, 'data': {'value': value,
                            'time': '2026-11-17T10:00:30.100Z',
                            'tags': {'phase': 'measured', 'scenario': scenario}}})
            self.events[agent] = events
            self.write_stream(agent)
            native = {'metrics': {
                'http_reqs': {'count': 110, 'rate': 3.55},
                'dropped_iterations': {'count': 2},
                'http_req_failed': {'rate': .1, 'passes': 11, 'fails': 99},
                'sideband_decision_success': {'rate': 1, 'passes': 110, 'fails': 0},
                'sideband_decision_latency': {'avg': 5, 'min': 1, 'med': 4, 'max': 10, 'p(90)': 6, 'p(95)': 8}}}
            (self.run / f'agent-{agent}-summary.json').write_text(json.dumps(native))

    def write_stream(self, agent, compressed=False):
        path = self.run / f'agent-{agent}-metrics.json'
        raw = ''.join(json.dumps(e) + '\n' for e in self.events[agent])
        if compressed:
            path.unlink(missing_ok=True)
            with gzip.open(str(path) + '.gz', 'wt') as f: f.write(raw)
        else:
            path.write_text(raw)

    def generate(self):
        (self.run / 'env.json').write_text(json.dumps(self.meta))
        old = sys.argv
        try:
            sys.argv = ['generate-report.py', 'run-1']
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                report.main()
        finally:
            sys.argv = old
        return json.loads((self.run / 'summary.json').read_text())

    def test_native_without_text_logs(self):
        s = self.generate()
        self.assertEqual(s['agents']['agent-0']['exit_code'], 0)
        self.assertIsNotNone(s['agents']['agent-0']['native_summary'])
        self.assertEqual(s['agents']['agent-1']['exit_code'], 0)
        # notes are coverage observations; execution/summary gaps absent
        self.assertFalse(any('execution exit code unavailable' in x for x in s['limitations']))
        self.assertFalse(any('structured k6 summary unavailable' in x for x in s['limitations']))

    def test_native_values_wrapped_format(self):
        p = self.run / 'agent-0-summary.json'
        native = json.loads(p.read_text())
        native['metrics'] = {k: {'values': v} for k, v in native['metrics'].items()}
        p.write_text(json.dumps(native))
        self.assertEqual(self.generate()['dropped_iterations'], 4)

    def test_mixed_empty_stream_preserves_report(self):
        self.events[1] = []
        self.write_stream(1)
        s = self.generate()
        self.assertIn('agent-1: no request latency samples collected', s['limitations'])
        self.assertEqual(s['requests_observed'], 100)

    def test_all_empty_streams(self):
        for agent in range(2):
            self.events[agent] = []
            self.write_stream(agent)
        s = self.generate()
        self.assertIsNone(s['overall']['latency'])
        self.assertEqual(s['requests_observed'], 0)

    def test_malformed_records_surface(self):
        # phase tags are deliberately ignored by the simplified generator
        # (whole-run window); malformed records must still surface.
        self.events[0][0]['data']['tags']['phase'] = 'bad'
        self.write_stream(0)
        with (self.run / 'agent-0-metrics.json').open('a') as f:
            f.write('{"type":"Point",broken}\n')
        s = self.generate()
        self.assertTrue(any('malformed records excluded' in x for x in s['limitations']))
        # the phase-tagged sample is still measured (no phase filtering)
        self.assertEqual(s['scenarios']['mix_read']['latency']['requests'], 50)

    def test_gzip_equivalence(self):
        plain = self.generate()
        for agent in range(2): self.write_stream(agent, compressed=True)
        compressed = self.generate()
        self.assertEqual(plain['overall']['latency'], compressed['overall']['latency'])
        self.assertEqual(compressed['requests_observed'], 200)

    def test_missing_native_is_visible(self):
        (self.run / 'agent-1-summary.json').unlink()
        s = self.generate()
        self.assertIn('agent-1: structured k6 summary unavailable', s['limitations'])
        self.assertEqual(s['agents']['agent-1']['exit_code'], 0)

    def test_pooled_scenario_percentile_not_worst_agent(self):
        # 25 fast samples vs a single slow sample: pooled p95 remains 5 ms.
        self.events[1] = [e for e in self.events[1] if e['data']['tags']['scenario'] != 'mix_read']
        self.events[1].append({'type': 'Point', 'metric': 'http_req_duration',
            'data': {'time': '2026-11-17T10:00:30.100Z', 'value': 900,
                     'tags': {'phase': 'measured', 'scenario': 'mix_read'}}})
        self.write_stream(1)
        self.assertEqual(self.generate()['scenarios']['mix_read']['latency']['p95_ms'], 5)

    def test_sampler_provenance(self):
        (self.run / 'usage-during-run.txt').write_text(''.join(
            f'=== 10:00:30 ===\npaz: pingauthorize-0 100m 30Mi\n' for _ in range(6)))
        self.assertTrue(self.generate()['cpu_source'] == 'usage-during-run.txt')

    def test_collection_incomplete_flag_surfaces(self):
        # The collector's explicit flag must surface even when every file
        # that landed looks complete (fifth review, finding 4): a missing
        # agent's data is invisible to the per-file checks.
        self.meta['collection_incomplete'] = True
        s = self.generate()
        self.assertTrue(s['collection_incomplete'])
        self.assertIn("collector reported collection incomplete", s['limitations'][0])
        html = (self.run / 'report.html').read_text()
        self.assertIn('Collection incomplete', html)
        # without the flag, no marker anywhere
        self.meta['collection_incomplete'] = False
        s = self.generate()
        self.assertFalse(s['collection_incomplete'])
        self.assertNotIn('Collection incomplete', (self.run / 'report.html').read_text())

    def test_response_timestamp_fields_are_epoch_seconds(self):
        # JSON names must describe their values (fifth review, finding 5):
        # epoch seconds, not the _utc misnomer.
        s = self.generate()
        a = s['agents']['agent-0']
        self.assertIn('first_response_epoch_s', a)
        self.assertIn('last_response_epoch_s', a)
        self.assertNotIn('first_response_utc', a)
        self.assertNotIn('last_response_utc', a)
        self.assertIsInstance(a['first_response_epoch_s'], float)
        self.assertIsInstance(a['last_response_epoch_s'], float)

    def test_cpu_clip_is_half_open(self):
        # A sample exactly at the lower bound is included; one exactly at
        # the upper bound (beginning of the next second) is excluded
        # (fifth review, finding 6).
        (self.run / 'usage-during-run.txt').write_text(
            '=== 10:00:30 ===\npaz: pingauthorize-0 100m 30Mi\n'   # == lower bound
            '=== 10:00:31 ===\npaz: pingauthorize-0 200m 30Mi\n')  # == upper bound
        s = self.generate()
        paz = [k for k in s['cpu'] if 'pingauthorize-0' in k]
        self.assertTrue(paz)
        samples = s['cpu'][paz[0]]['samples']
        self.assertEqual(samples, 1)  # lower bound in, upper bound out
        self.assertEqual(s['cpu'][paz[0]]['median_m'], 100.0)

    # --- policy-tree scaling campaign (2026-11-24) ------------------------

    def legacy_fixture_bytes(self):
        """The report the unmodified legacy fixture produces — the byte-shape
        guarantee for runs without branch tags."""
        self.generate()
        return (self.run / 'report.html').read_text()

    def test_legacy_stream_has_no_branch_section(self):
        legacy = self.legacy_fixture_bytes()
        # A stream carrying no branch tags must render no per-branch section.
        html = self.legacy_fixture_bytes()
        self.assertNotIn('Per-branch latency spread', html)
        # and the legacy report must be byte-identical across regenerations
        self.assertEqual(legacy, html)

    def test_branch_tags_render_decile_section(self):
        # Tag every duration sample with branch 1 (agent-0) or 100 (agent-1):
        # the section appears, and the decile table spans branches 1..100.
        for agent, branch in [(0, 1), (1, 100)]:
            for e in self.events[agent]:
                if e['metric'] == 'http_req_duration':
                    e['data']['tags']['branch'] = str(branch)
        self.write_stream(0)
        self.write_stream(1)
        html = self.legacy_fixture_bytes()
        self.assertIn('Per-branch latency spread', html)
        self.assertIn('branch 1', html)
        self.assertIn('branch 100', html)

    def test_branch_1_only_tags_render_no_section(self):
        # BRANCH_COUNT=1 runs tag everything branch=1: same shape as legacy,
        # no section.
        for agent in range(2):
            for e in self.events[agent]:
                if e['metric'] == 'http_req_duration':
                    e['data']['tags']['branch'] = '1'
        self.write_stream(0)
        self.write_stream(1)
        self.assertNotIn('Per-branch latency spread', self.legacy_fixture_bytes())

if __name__ == '__main__': unittest.main()
