import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError

from buyer_calibration.calibration import analyze, analyze_snapshot
from buyer_calibration.client import Client
from buyer_calibration.domain import matches
from buyer_calibration.repository import Conflict, Repository
from buyer_calibration.service import make_server


def load(name):
    return json.loads(Path('fixtures', name).read_text())


class CalibrationMathTests(unittest.TestCase):
    def setUp(self):
        self.demo = load('demo.json')
        self.history = load('history.json')

    def test_only_observed_cells_and_margins(self):
        result = analyze(self.history, {'bootstrap_replicates': 0})
        observed = {(row['region'], row['size'], row['sector'])
                    for row in self.history['respondents']}
        cell_keys = {tuple(cell['key']) for cell in result['data']['cells']}
        self.assertEqual(cell_keys, observed)
        self.assertEqual(len(result['data']['cells']), 8)
        self.assertEqual(result['data']['coverage']['missing_combinations'], 0)
        for margin in result['margins']:
            self.assertGreaterEqual(margin['achieved'], margin['lower'] - 1e-6)
            self.assertLessEqual(margin['achieved'], margin['upper'] + 1e-6)

    def test_identified_range_brackets_point_estimate(self):
        result = analyze(self.history, {'bootstrap_replicates': 0})
        low, high = result['overall']['identified_range']['mean']
        self.assertGreaterEqual(low, 0)
        self.assertLessEqual(low, high)
        self.assertGreaterEqual(result['overall']['calibrated_mean'], low - 1e-6)
        self.assertLessEqual(result['overall']['calibrated_mean'], high + 1e-6)

    def test_intersecting_margins_respected(self):
        result = analyze(self.demo, {'bootstrap_replicates': 0})
        self.assertEqual(result['diagnosis']['status'], 'unstable')
        for margin in result['margins']:
            self.assertLessEqual(margin['achieved'], margin['upper'] + 1e-6)

    def test_unsupported_combination_is_not_invented(self):
        changed = copy.deepcopy(self.history)
        changed['margins'] = [
            {'id': 'all', 'where': {}, 'lower': 100, 'upper': 100},
            {'id': 'north', 'where': {'region': 'north'}, 'lower': 40, 'upper': 40},
        ]
        result = analyze(changed, {'bootstrap_replicates': 0})
        self.assertEqual(result['diagnosis']['status'], 'conflict')
        self.assertIn('support_gap', result['diagnosis']['codes'])
        self.assertIsNone(result['overall'])

    def test_forced_intersection_without_cells_is_conflict(self):
        changed = copy.deepcopy(self.history)
        changed['respondents'] = [row for row in changed['respondents']
                                  if not (row['region'] == 'west' and row['size'] == 'small')]
        changed['margins'] = [
            {'id': 'all', 'where': {}, 'lower': 100, 'upper': 100},
            {'id': 'west', 'where': {'region': 'west'}, 'lower': 60, 'upper': 60},
            {'id': 'small', 'where': {'size': 'small'}, 'lower': 50, 'upper': 50},
        ]
        result = analyze(changed, {'bootstrap_replicates': 0})
        self.assertEqual(result['diagnosis']['status'], 'conflict')
        self.assertGreater(result['data']['coverage']['missing_combinations'], 0)
        self.assertIn(['all', 'west', 'small'], result['diagnosis']['conflict_sets'])
        self.assertIn('resolving_relaxations', result['diagnosis'])
        self.assertIsNone(result['overall'])

    def test_margin_conflict_is_reported_with_set(self):
        changed = copy.deepcopy(self.history)
        changed['margins'] = [
            {'id': 'all', 'where': {}, 'lower': 100, 'upper': 100},
            {'id': 'east', 'where': {'region': 'east'}, 'lower': 70, 'upper': 70},
            {'id': 'west', 'where': {'region': 'west'}, 'lower': 60, 'upper': 60},
        ]
        result = analyze(changed, {'bootstrap_replicates': 0})
        self.assertEqual(result['diagnosis']['status'], 'conflict')
        self.assertIn('margin_conflict', result['diagnosis']['codes'])
        self.assertIn(['all', 'east', 'west'], result['diagnosis']['conflict_sets'])

    def test_weight_cap_conflict_and_minimal_cap(self):
        result = analyze(self.history, {'weight_cap': 2, 'bootstrap_replicates': 0})
        self.assertEqual(result['diagnosis']['status'], 'unstable')
        self.assertIn('weight_cap_conflict', result['diagnosis']['codes'])
        self.assertAlmostEqual(result['calibration']['minimal_feasible_cap'], 2.857143, places=5)
        feasible = analyze(self.history, {'weight_cap': 3, 'bootstrap_replicates': 0})
        self.assertNotEqual(feasible['diagnosis']['status'], 'conflict')
        self.assertAlmostEqual(feasible['calibration']['max_weight_ratio'], 3.0, places=5)

    def test_weight_cap_monotone_effect(self):
        loose = analyze(self.history, {'weight_cap': 5, 'bootstrap_replicates': 0})
        tight = analyze(self.history, {'weight_cap': 3, 'bootstrap_replicates': 0})
        self.assertLessEqual(tight['overall']['max_weight_ratio'], 3.0 + 1e-6)
        self.assertLessEqual(loose['overall']['max_weight_ratio'], 5.0 + 1e-6)
        self.assertLess(tight['overall']['ess'], loose['overall']['ess'])
        self.assertGreater(tight['calibration']['minimal_feasible_cap'], 2.0)

    def test_tradeoffs_record_each_choice(self):
        result = analyze(self.history, {'bootstrap_replicates': 0})
        tradeoffs = result['tradeoffs']
        caps = [entry['weight_cap'] for entry in tradeoffs['cap_scenarios']]
        self.assertEqual(len(set(caps)), len(caps))
        self.assertTrue(any(not entry['feasible'] for entry in tradeoffs['cap_scenarios']))
        self.assertTrue(any(entry['feasible'] for entry in tradeoffs['cap_scenarios']))
        for entry in tradeoffs['cap_scenarios']:
            if entry['feasible']:
                self.assertIn('mean', entry)
                self.assertIn('ess', entry)
        for entry in tradeoffs['margin_relaxations']:
            self.assertIn('feasible', entry)
            self.assertIn('id', entry)
        self.assertFalse(tradeoffs['margin_endpoint_corners']['skipped'])
        low, high = tradeoffs['margin_endpoint_corners']['mean_range']
        self.assertLessEqual(low, high)

    def test_group_dependence_uses_cluster_bootstrap(self):
        result = analyze(self.history, {'bootstrap_replicates': 40, 'random_seed': 5})
        bootstrap = result['calibration']['bootstrap']
        self.assertEqual(bootstrap['replicates'], 40)
        self.assertGreater(bootstrap['successes'], 0)
        self.assertLessEqual(bootstrap['successes'], 40)
        self.assertIn('整群', bootstrap['note'])
        interval = result['overall']['bootstrap']
        self.assertLessEqual(interval['low'], interval['high'])
        self.assertGreaterEqual(interval['valid_replicates'], 1)

    def test_single_group_bootstrap_is_caveated(self):
        changed = copy.deepcopy(self.demo)
        for row in changed['respondents']:
            row['group'] = 'one'
        result = analyze(changed, {'bootstrap_replicates': 10})
        self.assertEqual(result['calibration']['bootstrap']['successes'], 0)
        self.assertIn('不足两个', result['calibration']['bootstrap']['note'])

    def test_coverage_and_thin_support(self):
        result = analyze(self.demo, {'bootstrap_replicates': 0})
        self.assertEqual(result['data']['coverage']['observed_cells'], 4)
        self.assertGreater(result['data']['coverage']['missing_combinations'], 0)
        self.assertTrue(any(entry['status'] in ('unstable', 'extrapolated')
                            for entry in result['targets']))

    def test_choices_and_provenance(self):
        result = analyze(self.history, {'bootstrap_replicates': 0})
        topics = {entry['topic'] for entry in result['choices']}
        self.assertEqual(topics, {'supported_cells', 'point_estimate', 'margins',
                                  'weight_cap', 'group_dependence', 'extrapolation'})
        self.assertIn('选择偏差假设', result['assumption'])
        self.assertEqual(result['meaning'], analyze(self.demo)['meaning'])

    def test_inputs_are_not_mutated(self):
        before = copy.deepcopy(self.history)
        analyze(self.history, {'bootstrap_replicates': 0})
        self.assertEqual(self.history, before)

    def test_unidentified_total_is_flagged(self):
        changed = copy.deepcopy(self.history)
        changed['margins'] = [
            {'id': 'east', 'where': {'region': 'east'}, 'lower': 40, 'upper': 45},
            {'id': 'large', 'where': {'size': 'large'}, 'lower': 30, 'upper': 35},
        ]
        result = analyze(changed, {'bootstrap_replicates': 0})
        self.assertIsNone(result['overall']['identified_range']['population'][1])
        self.assertIn('unidentified_total', result['overall']['codes'])
        bounded = analyze(self.history, {'weight_cap': 3, 'bootstrap_replicates': 0})
        self.assertEqual(bounded['overall']['identified_range']['population'], [100.0, 100.0])
        self.assertNotIn('unidentified_total', bounded['overall']['codes'])

    def test_single_margin_relaxation_is_not_offered(self):
        changed = copy.deepcopy(self.demo)
        changed['margins'] = [{'id': 'all', 'where': {}, 'lower': 100, 'upper': 100}]
        result = analyze(changed, {'bootstrap_replicates': 0})
        entry = result['tradeoffs']['margin_relaxations'][0]
        self.assertFalse(entry['feasible'])
        self.assertIn('唯一的边际', entry['note'])
        self.assertEqual(result['tradeoffs']['margin_endpoint_corners']['entries'], [])

    def test_identified_range_matches_feasible_sampling(self):
        from scipy.optimize import linprog
        import numpy as np
        from buyer_calibration import calibration as module
        cells = module._cells(self.history['respondents'], 1)
        margins = self.history['margins']
        matrix, lower, upper, _ = module._bounds(cells, margins, None)
        means = np.array([cell['mean'] for cell in cells])
        generator = np.random.default_rng(11)
        sampled = []
        for _ in range(150):
            objective = generator.normal(size=len(cells))
            result = linprog(objective, A_ub=np.vstack([matrix, -matrix]),
                             b_ub=np.r_[upper, -lower], bounds=[(0, None)] * len(cells),
                             method='highs')
            if result.status == 0 and result.x.sum() > 0:
                sampled.append(float(result.x @ means / result.x.sum()))
        target = {'id': 'all', 'where': {}}
        low = module._extreme(cells, margins, None, target, 'mean', False)
        high = module._extreme(cells, margins, None, target, 'mean', True)
        self.assertAlmostEqual(min(sampled), low, places=5)
        self.assertAlmostEqual(max(sampled), high, places=5)

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            analyze(self.history, {'unknown': 1})
        with self.assertRaises(ValueError):
            analyze(self.history, {'weight_cap': 0.5})
        with self.assertRaises(ValueError):
            analyze(self.history, {'margin_ids': ['missing']})
        with self.assertRaises(ValueError):
            analyze(self.history, {'targets': [{'id': 'x', 'where': {'unknown': 'y'}}]})
        with self.assertRaises(ValueError):
            analyze({**self.history, 'margins': []})

    def test_snapshot_binding_and_digest(self):
        digest = 'a' * 64
        snapshot = {'revision': 4, 'digest': digest, 'data': self.history}
        result = analyze_snapshot(snapshot, {'bootstrap_replicates': 0})
        self.assertEqual(result['revision'], 4)
        self.assertEqual(result['data_digest'], digest)
        self.assertEqual(len(result['analysis_digest']), 64)
        again = analyze_snapshot(snapshot, {'bootstrap_replicates': 0})
        self.assertEqual(result['analysis_digest'], again['analysis_digest'])


class CalibrationHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.database = Path(self.directory.name) / 'data.sqlite3'
        self.repository = Repository(self.database)
        self.server = make_server(self.repository)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(f'http://127.0.0.1:{self.server.server_port}')
        self.data = load('history.json')

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.directory.cleanup()

    def test_calibration_endpoint_and_revision(self):
        receipt = self.client.import_data(self.data, 'first')
        response = self.client.calibration(receipt['revision'], {'bootstrap_replicates': 0})
        self.assertEqual(response['revision'], receipt['revision'])
        self.assertEqual(response['digest'], receipt['digest'])
        self.assertEqual(response['result']['data_digest'], receipt['digest'])
        self.assertEqual(response['result']['revision'], receipt['revision'])
        with self.assertRaises(HTTPError) as caught:
            self.client.calibration(999)
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            self.client.request('/calibration?settings=not-json')
        self.assertEqual(caught.exception.code, 400)

    def test_calibration_does_not_follow_latest_revision(self):
        first = self.client.import_data(self.data, 'one')
        changed = copy.deepcopy(self.data)
        changed['respondents'][0]['intention'] = 5
        second = self.client.import_data(changed, 'two')
        pinned = self.client.calibration(first['revision'], {'bootstrap_replicates': 0})
        latest = self.client.calibration(None, {'bootstrap_replicates': 0})
        self.assertEqual(pinned['revision'], first['revision'])
        self.assertEqual(latest['revision'], second['revision'])
        self.assertEqual(pinned['result']['data_digest'], first['digest'])
        self.assertNotEqual(pinned['result']['analysis_digest'], latest['result']['analysis_digest'])

    def test_saved_analysis_roundtrip(self):
        receipt = self.client.import_data(self.data, 'first')
        saved = self.client.save_analysis('review-2026-11', receipt['revision'],
                                          {'weight_cap': 3, 'bootstrap_replicates': 0})
        self.assertEqual(saved['revision'], receipt['revision'])
        self.assertEqual(saved['data_digest'], receipt['digest'])
        listing = self.client.analyses(receipt['revision'])
        self.assertEqual(len(listing['analyses']), 1)
        stored = self.client.analysis(saved['analysis_id'])
        self.assertEqual(stored['revision'], receipt['revision'])
        self.assertEqual(stored['settings']['weight_cap'], 3)
        self.assertEqual(stored['settings']['bootstrap_replicates'], 0)
        self.assertIn('ess_threshold', stored['settings'])
        self.assertEqual(stored['digest'], saved['digest'])
        replay = self.client.save_analysis('review-2026-11', receipt['revision'],
                                           {'weight_cap': 3, 'bootstrap_replicates': 0})
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['analysis_id'], saved['analysis_id'])

    def test_saved_analysis_conflicts(self):
        receipt = self.client.import_data(self.data, 'first')
        self.client.save_analysis('fixed-key', receipt['revision'], {'bootstrap_replicates': 0})
        with self.assertRaises(HTTPError) as caught:
            self.client.save_analysis('fixed-key', receipt['revision'],
                                       {'bootstrap_replicates': 0, 'weight_cap': 3})
        self.assertEqual(caught.exception.code, 409)
        with self.assertRaises(HTTPError) as caught:
            self.client.save_analysis('other-key', 99, {'bootstrap_replicates': 0})
        self.assertEqual(caught.exception.code, 404)

    def test_analysis_survives_restart(self):
        receipt = self.client.import_data(self.data, 'first')
        saved = self.client.save_analysis('keep', receipt['revision'], {'bootstrap_replicates': 0})
        restarted = Repository(self.database)
        stored = restarted.analysis(saved['analysis_id'])
        self.assertEqual(stored['revision'], 1)
        self.assertEqual(stored['result']['data_digest'], receipt['digest'])
        with self.assertRaises(LookupError):
            restarted.analysis(999)

    def test_repository_refuses_mismatched_digest(self):
        receipt = self.repository.import_data(self.data, 'first')
        snapshot = self.repository.get(receipt['revision'])
        analysis = analyze_snapshot(snapshot, {'bootstrap_replicates': 0})
        mismatched = copy.deepcopy(analysis)
        mismatched['data_digest'] = 'b' * 64
        with self.assertRaises(Conflict):
            self.repository.save_analysis(mismatched, 'bad')


if __name__ == '__main__':
    unittest.main()
