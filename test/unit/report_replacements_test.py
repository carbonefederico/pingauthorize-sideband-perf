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
        self.assertEqual(s['integrity']['collection']['issues'], [])
        self.assertEqual(s['dropped_iterations'], 4)
        self.assertEqual(s['total_requests_measured'], 200)
        self.assertEqual(s['pooled_elapsed']['p95_ms'], 7)
        self.assertEqual(s['integrity']['http_transport_issues']['agent-0']['count'], 11)
        self.assertNotIn('verdict', s)

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
        self.assertIn('agent-1: zero measured samples', s['collection']['issues'])
        self.assertEqual(s['total_requests_measured'], 100)

    def test_all_empty_streams(self):
        for agent in range(2):
            self.events[agent] = []
            self.write_stream(agent)
        s = self.generate()
        self.assertIsNone(s['pooled_latency'])
        self.assertEqual(s['achieved_vs_target_pct'], 0)

    def test_bad_phase_and_malformed_records_surface(self):
        self.events[0][0]['data']['tags']['phase'] = 'bad'
        self.write_stream(0)
        with (self.run / 'agent-0-metrics.json').open('a') as f:
            f.write('{"type":"Point",broken}\n')
        s = self.generate()
        self.assertTrue(any('bad_phase_points' in x for x in s['collection']['issues']))
        self.assertTrue(any('malformed_points' in x for x in s['collection']['issues']))

    def test_gzip_equivalence(self):
        plain = self.generate()
        for agent in range(2): self.write_stream(agent, compressed=True)
        compressed = self.generate()
        self.assertEqual(plain['pooled_latency'], compressed['pooled_latency'])
        self.assertEqual(compressed['total_requests_measured'], 200)

    def test_missing_native_is_visible(self):
        (self.run / 'agent-1-summary.json').unlink()
        s = self.generate()
        self.assertIn('structured k6 summary missing: agent-1', s['collection']['issues'])
        self.assertEqual(s['execution']['per_agent']['agent-1']['exit_code'], 0)

    def test_pooled_scenario_percentile_not_worst_agent(self):
        # 25 fast samples vs a single slow sample: pooled p95 remains 5 ms.
        self.events[1] = [e for e in self.events[1] if e['data']['tags']['scenario'] != 'mix_read']
        self.events[1].append({'type': 'Point', 'metric': 'http_req_duration',
            'data': {'time': '2026-11-17T10:00:30.100Z', 'value': 900,
                     'tags': {'phase': 'measured', 'scenario': 'mix_read'}}})
        self.write_stream(1)
        self.assertEqual(self.generate()['scenario_stats']['mix_read']['p95_ms'], 5)

    def test_sampler_provenance(self):
        (self.run / 'usage-during-run.txt').write_text(''.join(
            f'=== 10:00:30 ===\npaz: pingauthorize-0 100m 30Mi\n' for _ in range(6)))
        self.assertTrue(self.generate()['cpu']['source'].endswith('usage-during-run.txt'))

if __name__ == '__main__': unittest.main()
