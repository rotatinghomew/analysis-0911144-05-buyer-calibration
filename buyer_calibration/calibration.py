"""可解释的边际校准分析。

模型只在样本实际观察到的（地区、规模、行业）单元格上分配总体企业数，
不创建没有样本支持的组合，也不允许用无限大的权重把缺失组合“补”出来。
每个单元格的校准总量 x_c 满足已知边际区间（边际之间可以交叠），并可选择
限制 x_c <= cap * 既有设计权重。点估计取离既有设计权重最近的可行解。

输出同时给出：
  * 总体与指定业务关注人群的意向均值 / 总额；
  * 仅由约束决定的识别区间（不依赖点估计的求解方式）；
  * 有效信息量（企业级与集团级有效样本量、最大权重膨胀）；
  * 集团共同回应假设下的整群重抽样区间；
  * 收紧权重上限、放宽各项边际的代价比较，以及最小可行上限；
  * 每次分析所用的资料版本、设置与建模选择，供区域团队复核。

结论始终是“在透明的校准与选择偏差假设下”的估计，不是对总体的观测。
"""

import hashlib
import itertools
import json
import math
import warnings

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, linprog, minimize

from .common import finite, require
from .domain import matches

FIELDS = ('region', 'size', 'sector')

DEFAULTS = {
    'targets': None,
    'margin_ids': None,
    'weight_cap': None,
    'min_cell_respondents': 1,
    'bootstrap_replicates': 200,
    'random_seed': 20261115,
    'ess_threshold': 20.0,
    'group_ess_threshold': 10.0,
    'range_width_threshold': 0.25,
    'bootstrap_width_threshold': 0.30,
    'weight_inflation_threshold': 5.0,
    'thin_support_share_threshold': 0.25,
    'bootstrap_infeasible_threshold': 0.20,
    'max_corner_margins': 8,
}

CAP_SCENARIOS = (1.0, 1.25, 1.5, 2.0, 3.0, 5.0)

ASSUMPTION = (
    '单元格内的回复企业被当作该单元格未回复企业的代表；没有抽样概率时这是选择偏差假设，'
    '校准不能宣称已消除自愿回复偏差。'
)

MEANING = (
    '在声明版本资料、所选边际与权重上限下求得的校准估计，不是对总体的直接观测；'
    '识别区间只反映约束能确定的范围，不包含假设误差。'
)


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return round(number, 6) if number != 0 else 0.0
    return value


def _settings(raw):
    if raw is None:
        raw = {}
    require(isinstance(raw, dict), 'settings 必须是对象')
    unknown = set(raw) - set(DEFAULTS)
    require(not unknown, f'settings 含未知字段: {sorted(unknown)}')
    settings = dict(DEFAULTS)
    settings.update(raw)
    if settings['weight_cap'] is not None:
        finite(settings['weight_cap'], 'weight_cap')
        require(settings['weight_cap'] >= 1, 'weight_cap 不得小于 1')
    for name, minimum in (('min_cell_respondents', 1), ('bootstrap_replicates', 0),
                          ('max_corner_margins', 0), ('random_seed', None)):
        value = settings[name]
        require(not isinstance(value, bool) and isinstance(value, int), f'{name} 必须是整数')
        require(minimum is None or value >= minimum, f'{name} 低于允许范围')
    require(settings['bootstrap_replicates'] <= 5000, 'bootstrap_replicates 过大')
    for name in ('ess_threshold', 'group_ess_threshold', 'range_width_threshold',
                 'bootstrap_width_threshold', 'weight_inflation_threshold',
                 'thin_support_share_threshold', 'bootstrap_infeasible_threshold'):
        finite(settings[name], name)
        require(settings[name] >= 0, f'{name} 不得为负')
    for name in ('range_width_threshold', 'bootstrap_width_threshold',
                 'weight_inflation_threshold'):
        require(settings[name] > 0, f'{name} 必须为正')
    for name in ('bootstrap_infeasible_threshold',):
        require(settings[name] <= 1, f'{name} 必须是不超过 1 的比例')
    ids = settings['margin_ids']
    if ids is not None:
        require(isinstance(ids, list) and ids and all(isinstance(item, str) and item for item in ids),
                'margin_ids 必须是非空标识数组')
        require(len(ids) == len(set(ids)), 'margin_ids 重复')
    targets = settings['targets']
    if targets is not None:
        require(isinstance(targets, list) and targets, 'targets 必须是非空数组')
        for target in targets:
            require(isinstance(target, dict) and set(target) == {'id', 'where'},
                    'target 必须且只能含 id 与 where')
            require(isinstance(target['id'], str) and target['id'], 'target.id 不能为空')
            require(isinstance(target['where'], dict), 'target.where 必须是对象')
            require(set(target['where']) <= set(FIELDS), 'target.where 含不支持的字段')
            require(all(isinstance(value, str) and value for value in target['where'].values()),
                    'target.where 的值必须是非空字符串')
        require(len({target['id'] for target in targets}) == len(targets), 'target.id 重复')
    return settings


def _levels(rows):
    return {field: sorted({row[field] for row in rows}) for field in FIELDS}


def _default_targets(rows):
    levels = _levels(rows)
    targets = [{'id': 'all', 'where': {}}]
    for field in FIELDS:
        for value in levels[field]:
            targets.append({'id': f'{field}={value}', 'where': {field: value}})
    return targets


def _cells(rows, minimum):
    buckets = {}
    for row in rows:
        key = tuple(row[field] for field in FIELDS)
        bucket = buckets.setdefault(key, {'rows': [], 'groups': set(), 'design': 0.0, 'amount': 0.0})
        bucket['rows'].append(row)
        bucket['groups'].add(row['group'])
        bucket['design'] += row['base_weight']
        bucket['amount'] += row['intention'] * row['base_weight']
    cells = []
    for key, bucket in buckets.items():
        if len(bucket['rows']) < minimum or bucket['design'] <= 0:
            continue
        cells.append({
            'key': key,
            'where': dict(zip(FIELDS, key)),
            'rows': bucket['rows'],
            'respondents': len(bucket['rows']),
            'groups': len(bucket['groups']),
            'design': bucket['design'],
            'mean': bucket['amount'] / bucket['design'],
        })
    return sorted(cells, key=lambda cell: cell['key'])


def _margin_matrix(cells, margins):
    return np.array([
        [1.0 if matches(cell['where'], margin['where']) else 0.0 for cell in cells]
        for margin in margins
    ], dtype=float).reshape(len(margins), len(cells))


def _bounds(cells, margins, cap):
    matrix = _margin_matrix(cells, margins)
    lower = np.array([margin['lower'] for margin in margins], dtype=float)
    upper = np.array([margin['upper'] for margin in margins], dtype=float)
    design = np.array([cell['design'] for cell in cells], dtype=float)
    return matrix, lower, upper, design


def _cap_limit(cells, cap):
    if cap is None:
        return None
    return np.array([cap * cell['design'] for cell in cells], dtype=float)


def _linear_program(cells, margins, cap, fixed=None, ratio_mask=None,
                    ratio_scale=False, maximize=False, coefficient=None):
    """统一线性规划；ratio_scale 时用 Charnes-Cooper 变换求比值的极值。

    coefficient 为逐单元格的目标系数（总额口径）；比值口径下自动归一。
    """
    matrix, lower, upper, design = _bounds(cells, margins, cap)
    count = len(cells)
    limit = _cap_limit(cells, cap)
    if fixed:
        lower = lower.copy()
        upper = upper.copy()
        for index, value in fixed.items():
            lower[index] = value
            upper[index] = value
    coefficient = np.zeros(count) if coefficient is None else np.asarray(coefficient, dtype=float)
    if ratio_scale:
        rows = [np.c_[matrix, -upper], np.c_[-matrix, lower]]
        rhs = [np.zeros(len(margins)), np.zeros(len(margins))]
        if limit is not None:
            rows.append(np.c_[np.eye(count), -limit])
            rhs.append(np.zeros(count))
        equality = np.zeros((1, count + 1))
        equality[0, np.asarray(ratio_mask, dtype=bool)] = 1.0
        objective = np.zeros(count + 1)
        objective[:count] = -coefficient if maximize else coefficient
        result = linprog(objective, A_ub=np.vstack(rows), b_ub=np.concatenate(rhs),
                         A_eq=equality, b_eq=np.array([1.0]),
                         bounds=[(0, None)] * count + [(0, None)], method='highs')
        if result.status != 0 or result.x[count] <= 0:
            return None
        return result.x[:count]
    rows = [matrix, -matrix]
    rhs = [upper, -lower]
    variable_bounds = [(0, None if limit is None else float(limit[index])) for index in range(count)]
    objective = -coefficient if maximize else coefficient
    result = linprog(objective, A_ub=np.vstack(rows), b_ub=np.concatenate(rhs),
                     bounds=variable_bounds, method='highs')
    if result.status != 0:
        return None
    return result.x


def _feasible(cells, margins, cap):
    if not cells:
        return False, None
    solution = _linear_program(cells, margins, cap)
    return solution is not None, solution


def _closest(cells, margins, cap, fixed=None):
    """在可行域内取离既有设计权重最近的解；返回 (解, 是否收敛, 说明)。"""
    if not cells:
        return None, False, '没有可用单元格'
    matrix, lower, upper, design = _bounds(cells, margins, cap)
    start = _linear_program(cells, margins, cap, fixed=fixed)
    if start is None:
        return None, False, '约束下无可行解'
    if fixed:
        lower = lower.copy()
        upper = upper.copy()
        for index, value in fixed.items():
            lower[index] = value
            upper[index] = value
    limit = _cap_limit(cells, cap)
    rows = [matrix] if len(margins) else []
    lows = [lower] if len(margins) else []
    highs = [upper] if len(margins) else []
    if limit is not None:
        rows.append(np.eye(len(cells)))
        lows.append(np.full(len(cells), -np.inf))
        highs.append(limit)
    if not rows:
        return start, True, ''
    constraint = LinearConstraint(np.vstack(rows), np.concatenate(lows), np.concatenate(highs))

    def objective(x):
        return float(np.sum((x - design) ** 2 / design))

    def jacobian(x):
        return 2.0 * (x - design) / design

    def hessian(x):
        return np.diag(2.0 / design)

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        solved = minimize(objective, start, jac=jacobian, hess=hessian, constraints=constraint,
                          bounds=Bounds(np.zeros(len(cells)), np.full(len(cells), np.inf)),
                          method='trust-constr',
                          options={'maxiter': 8000, 'gtol': 1e-10, 'xtol': 1e-13,
                                   'barrier_tol': 1e-10})
    x = solved.x
    violation = float(max(np.max(-x), 0.0)) if len(x) else 0.0
    if len(lower):
        violation = max(violation, float(np.max(matrix @ x - upper)),
                        float(np.max(lower - matrix @ x)))
    if limit is not None:
        violation = max(violation, float(np.max(x - limit)))
    if not math.isfinite(objective(x)) or violation > 1e-5:
        return start, False, '权重求解未完全收敛，回退到可行初始解'
    return x, True, ''


def _ess(weights):
    weights = np.asarray(weights, dtype=float)
    weights = weights[weights > 0]
    if len(weights) == 0:
        return 0.0
    return float(weights.sum() ** 2 / np.sum(weights ** 2))


def _matching(cells, target):
    return [index for index, cell in enumerate(cells) if matches(cell['where'], target['where'])]


def _target_ess(cells, x, matching):
    weights = []
    group_weights = {}
    for index in matching:
        factor = x[index] / cells[index]['design']
        for row in cells[index]['rows']:
            weight = factor * row['base_weight']
            weights.append(weight)
            group_weights[row['group']] = group_weights.get(row['group'], 0.0) + weight
    return _ess(weights), _ess(list(group_weights.values())), len(group_weights)


def _statistics(cells, x, target):
    matching = _matching(cells, target)
    if not matching:
        return None
    population = float(sum(x[index] for index in matching))
    if population <= 0:
        return None
    design = float(sum(cells[index]['design'] for index in matching))
    amount = float(sum(x[index] * cells[index]['mean'] for index in matching))
    design_amount = float(sum(cells[index]['design'] * cells[index]['mean'] for index in matching))
    respondents = sum(cells[index]['respondents'] for index in matching)
    ess, group_ess, groups = _target_ess(cells, x, matching)
    thin = sum(x[index] for index in matching
               if cells[index]['respondents'] < 3 or cells[index]['groups'] < 2)
    ratios = [x[index] / cells[index]['design'] for index in matching]
    return {
        'population': population,
        'respondents': respondents,
        'groups': groups,
        'design_weight': design,
        'design_mean': design_amount / design if design else None,
        'calibrated_mean': amount / population,
        'total_amount': amount,
        'shift_vs_design': (amount / population - design_amount / design) if design else None,
        'ess': ess,
        'group_ess': group_ess,
        'max_weight_ratio': max(ratios),
        'min_weight_ratio': min(ratios),
        'thin_support_share': thin / population,
        'population_per_respondent': population / respondents if respondents else None,
        'cells_used': len(matching),
    }


def _extreme(cells, margins, cap, target, statistic, maximize):
    matching = _matching(cells, target)
    if not matching:
        return None
    means = np.array([cell['mean'] for cell in cells])
    coefficient = np.zeros(len(cells))
    coefficient[matching] = means[matching]
    if statistic == 'mean':
        mask = np.zeros(len(cells) + 1, dtype=float)
        mask[matching] = 1.0
        solution = _linear_program(cells, margins, cap, ratio_mask=mask, ratio_scale=True,
                                   maximize=maximize, coefficient=coefficient)
        if solution is None:
            return None
        return float(sum(means[index] * solution[index] for index in matching))
    solution = _linear_program(cells, margins, cap, maximize=maximize,
                               coefficient=coefficient)
    if solution is None:
        return None
    return float(sum(means[index] * solution[index] for index in matching))


def _conflict_sets(cells, margins, limit=5):
    """找出最小的不可行边际子集；只报告规模最小的那些，避免把整组都列为冲突源。"""
    if _feasible(cells, margins, None)[0]:
        return []
    for size in range(1, min(len(margins), limit) + 1):
        found = []
        for subset in itertools.combinations(margins, size):
            if not _feasible(cells, list(subset), None)[0]:
                found.append([margin['id'] for margin in subset])
        if found:
            return found
    return [[margin['id'] for margin in margins]]


def _unobserved_levels(rows, cells, margins, minimum):
    """在观测层级中存在、但没有任何可用单元格的边际；这类约束无法满足。"""
    missing = []
    present = {cell['where'][field] for cell in cells for field in cell['where']} if cells else set()
    for margin in margins:
        if not margin['where']:
            continue
        if len(margin['where']) != 1:
            continue
        field, value = next(iter(margin['where'].items()))
        if value in {row[field] for row in rows} and value not in present:
            matching = [cell for cell in cells if matches(cell['where'], margin['where'])]
            if not matching and any(matches(row, margin['where']) for row in rows):
                missing.append(margin['id'])
    return missing


def _forced_unobserved(cells, margins, cap):
    """在可行域内找出没有任何观测单元格、却被边际约束要求非零企业数的组合。"""
    if not cells or not _feasible(cells, margins, cap)[0]:
        return []
    matrix, lower, upper, design = _bounds(cells, margins, cap)
    limit = _cap_limit(cells, cap)
    level_values = {field: sorted({cell['where'][field] for cell in cells}) for field in FIELDS}
    found = []
    for size in (1, 2, 3):
        for fields in itertools.combinations(FIELDS, size):
            for values in itertools.product(*(level_values[field] for field in fields)):
                condition = dict(zip(fields, values))
                if any(matches(cell['where'], condition) for cell in cells):
                    continue
                coefficient = np.array([1.0 if matches(cell['where'], condition) else 0.0
                                        for cell in cells])
                result = linprog(-coefficient, A_ub=np.vstack([matrix, -matrix]),
                                 b_ub=np.r_[upper, -lower],
                                 bounds=[(0, None if limit is None else float(limit[index]))
                                         for index in range(len(cells))], method='highs')
                if result.status == 0:
                    forced = float(np.sum(coefficient * result.x))
                    if forced > 1e-6:
                        found.append({'where': condition, 'forced_min': forced})
    return found


def _diagnose(cells, margins, cap, rows=None, minimum=1):
    if not cells:
        return 'conflict', ['no_support'], '最小样本量过滤后没有任何可用单元格，无法校准。', {}
    if _feasible(cells, margins, cap)[0]:
        return 'ok', [], '', {}
    individual = []
    for margin in margins:
        matching = [cell for cell in cells if matches(cell['where'], margin['where'])]
        individual.append({
            'id': margin['id'], 'where': margin['where'],
            'feasible_alone': _feasible(cells, [margin], None)[0],
            'observed_cells': len(matching),
            'observed_respondents': sum(cell['respondents'] for cell in matching),
            'lower': margin['lower'], 'upper': margin['upper'],
        })
    details = {'margins': individual}
    if not _feasible(cells, margins, None)[0]:
        unsupported = [item['id'] for item in individual if item['observed_cells'] == 0]
        gaps = _unobserved_levels(rows, cells, margins, minimum) if rows is not None else []
        forced = _forced_unobserved(cells, margins, cap)
        conflicts = _conflict_sets(cells, margins)
        details['conflict_sets'] = conflicts
        if forced:
            details['forced_unobserved'] = forced
            names = ['/'.join(f'{key}={value}' for key, value in item['where'].items())
                     for item in forced]
            return ('conflict', ['support_gap'],
                    f'约束把企业数强制加在没有观测单元格的组合上：{names[:5]}；'
                    '这些组合没有样本支持，不能用巨大权重冒充已观察。请修正边际或补充样本。', details)
        if gaps:
            details['unobserved_levels'] = gaps
            return ('conflict', ['support_gap'],
                    f'边际 {gaps} 对应的层级存在回复企业，但合并后没有任何可用单元格（可能被最小样本量过滤）；'
                    '这些企业不能靠巨大权重补足。请检查过滤阈值或补充样本。', details)
        if unsupported:
            return ('conflict', ['support_gap'],
                    f'边际 {unsupported} 在观测不到的单元格上要求企业数；样本中没有这些组合，'
                    '不能用巨大权重或虚拟组合冒充已观察。请修正边际或补充样本。', details)
        resolvers = []
        for candidate in margins:
            remaining = [item for item in margins if item['id'] != candidate['id']]
            if remaining and _feasible(cells, remaining, None)[0]:
                resolvers.append(candidate['id'])
        details['resolving_relaxations'] = resolvers
        hint = f'单独去掉 {resolvers} 任一项后可恢复可行。' if resolvers else ''
        return ('conflict', ['margin_conflict'],
                f'各边际单独可行，但这些边际的组合互相冲突：{conflicts}。{hint}'
                '需放宽其中一项、收窄区间或修改边际定义。', details)
    minimal = _minimal_cap(cells, margins)
    details['minimal_feasible_cap'] = minimal
    return ('unstable', ['weight_cap_conflict'],
            f'边际本身可行，但在单元格权重上限 {cap} 下无解；'
            f'最小可行上限约为 {minimal:.3f}，放宽上限才能校准。', details)


def _minimal_cap(cells, margins):
    """最小可行的单元格权重倍数：min t, s.t. x_i <= t * design_i。"""
    if not _feasible(cells, margins, None)[0]:
        return None
    matrix, lower, upper, design = _bounds(cells, margins, None)
    count = len(cells)
    zeros = np.zeros((len(margins), count + 1))
    rows = [np.c_[matrix, zeros[:, :1]], np.c_[-matrix, zeros[:, :1]]]
    bounds = [upper, -lower]
    for index in range(count):
        row = np.zeros(count + 1)
        row[index] = 1.0
        row[count] = -design[index]
        rows.append(row.reshape(1, -1))
        bounds.append(np.zeros(1))
    objective = np.zeros(count + 1)
    objective[count] = 1.0
    result = linprog(objective, A_ub=np.vstack(rows), b_ub=np.concatenate(bounds),
                     bounds=[(0, None)] * count + [(1.0, None)], method='highs')
    if result.status != 0:
        return None
    return float(result.x[count])


def _total_bounds(cells, margins, cap):
    """总体企业数的识别范围；两端不相等说明所选边际没有锁定总体规模。"""
    if not cells:
        return None, None
    coefficient = np.ones(len(cells))
    upper_solution = _linear_program(cells, margins, cap, maximize=True, coefficient=coefficient)
    lower_solution = _linear_program(cells, margins, cap, coefficient=coefficient)
    if lower_solution is None:
        return None, None
    lower = float(np.sum(lower_solution))
    upper = float(np.sum(upper_solution)) if upper_solution is not None else None
    return lower, upper


def _overall_mean(cells, x):
    population = float(np.sum(x))
    if population <= 0:
        return None
    return float(np.sum(x * np.array([cell['mean'] for cell in cells])) / population)


def _weights(cells, x):
    weights = []
    for index, cell in enumerate(cells):
        factor = x[index] / cell['design']
        weights.extend(factor * row['base_weight'] for row in cell['rows'])
    return weights


def _bootstrap(rows, margins, cap, targets, minimum, replicates, seed):
    if replicates <= 0:
        return None
    by_group = {}
    for row in rows:
        by_group.setdefault(row['group'], []).append(row)
    names = sorted(by_group)
    if len(names) < 2:
        return {'replicates': replicates, 'successes': 0, 'infeasible': replicates,
                'infeasible_share': 1.0, 'intervals': {},
                'note': '可重抽样的集团不足两个，无法评估共同回应带来的不确定性。'}
    generator = np.random.default_rng(seed)
    samples = {target['id']: [] for target in targets}
    successes = 0
    failures = 0
    for _ in range(replicates):
        pick = generator.integers(0, len(names), size=len(names))
        sampled = [row for index in pick for row in by_group[names[index]]]
        cells = _cells(sampled, minimum)
        if not _feasible(cells, margins, cap)[0]:
            failures += 1
            continue
        x, _, _ = _closest(cells, margins, cap)
        if x is None:
            failures += 1
            continue
        successes += 1
        for target in targets:
            stats = _statistics(cells, x, target)
            if stats is not None:
                samples[target['id']].append(stats['calibrated_mean'])
    intervals = {}
    for target_id, values in samples.items():
        if values:
            intervals[target_id] = {
                'low': float(np.percentile(values, 2.5)),
                'high': float(np.percentile(values, 97.5)),
                'valid_replicates': len(values),
            }
    return {'replicates': replicates, 'successes': successes, 'infeasible': failures,
            'infeasible_share': failures / replicates, 'intervals': intervals,
            'note': '按集团整群重抽样；区间包含重抽样不稳定性，不修正选择偏差。'}


def _relative(value, width):
    return width / abs(value) if value else float('inf')


def _assess(stats, settings, finite_range=None, boot=None, bootstrap=None, identified=True):
    codes = []
    if stats is None:
        return 'conflict', ['no_support']
    if not identified:
        codes.append('unidentified_total')
    if stats['ess'] < settings['ess_threshold'] or stats['group_ess'] < settings['group_ess_threshold']:
        codes.append('low_effective_information')
    if stats['max_weight_ratio'] > settings['weight_inflation_threshold']:
        codes.append('weight_inflation')
    if finite_range and None not in finite_range and stats['calibrated_mean']:
        width = finite_range[1] - finite_range[0]
        if _relative(stats['calibrated_mean'], width) > settings['range_width_threshold']:
            codes.append('wide_identified_range')
    if boot and stats['calibrated_mean']:
        width = boot['high'] - boot['low']
        if _relative(stats['calibrated_mean'], width) > settings['bootstrap_width_threshold']:
            codes.append('wide_bootstrap_interval')
    if bootstrap and bootstrap['infeasible_share'] > settings['bootstrap_infeasible_threshold']:
        codes.append('bootstrap_infeasible')
    if codes:
        return 'unstable', codes
    if stats['thin_support_share'] > settings['thin_support_share_threshold']:
        return 'extrapolated', ['thin_support']
    return 'calibrated', []


def _cap_scenarios(cells, margins, cap):
    """比较不同单元格权重上限的可行性与代价；第一项为请求值。"""
    unrestricted = None
    if _feasible(cells, margins, None)[0]:
        solution, _, _ = _closest(cells, margins, None)
        if solution is not None:
            unrestricted = _overall_mean(cells, solution)
    entries = []
    seen = set()
    candidates = ([cap] if cap is not None else []) + list(CAP_SCENARIOS)
    for scenario in candidates:
        if scenario in seen:
            continue
        seen.add(scenario)
        available, _ = _feasible(cells, margins, scenario)
        entry = {'weight_cap': scenario, 'feasible': available,
                 'is_requested': scenario == cap}
        if available:
            solution, converged, note = _closest(cells, margins, scenario)
            if solution is not None:
                entry.update({
                    'mean': _overall_mean(cells, solution),
                    'ess': _ess(_weights(cells, solution)),
                    'max_weight_ratio': max(solution[index] / cells[index]['design']
                                            for index in range(len(cells))),
                    'delta_vs_unrestricted': (None if unrestricted is None
                                              else _overall_mean(cells, solution) - unrestricted),
                    'converged': converged,
                })
                if note:
                    entry['note'] = note
        entries.append(entry)
    return entries, unrestricted


def _margin_relaxations(cells, margins, cap):
    entries = []
    for margin in margins:
        remaining = [candidate for candidate in margins if candidate['id'] != margin['id']]
        entry = {'id': margin['id']}
        if not remaining:
            entry.update({'feasible': False,
                          'note': '这是唯一的边际，去掉后没有约束可识别总体，不能作为放宽选项。'})
            entries.append(entry)
            continue
        available = _feasible(cells, remaining, cap)[0]
        entry['feasible'] = available
        if available:
            solution, converged, _ = _closest(cells, remaining, cap)
            if solution is not None:
                entry.update({'mean': _overall_mean(cells, solution),
                              'ess': _ess(_weights(cells, solution)), 'converged': converged})
        else:
            entry['note'] = '去掉该项后其余约束无解，不能作为放宽选项。'
        entries.append(entry)
    return entries


def _corners(cells, margins, cap, settings):
    movable = [index for index, margin in enumerate(margins) if margin['upper'] > margin['lower']]
    if not movable:
        return {'skipped': False, 'entries': [], 'mean_range': None,
                'note': '所有边际都是点约束，无端点组合可比较。'}
    if len(movable) > settings['max_corner_margins']:
        return {'skipped': True, 'entries': [], 'mean_range': None,
                'note': f'非退化边际有 {len(movable)} 项，超过上限 {settings["max_corner_margins"]}，跳过端点组合。'}
    entries = []
    means = []
    for choice in itertools.product((0, 1), repeat=len(movable)):
        fixed = {index: margins[index]['lower'] if pick == 0 else margins[index]['upper']
                 for index, pick in zip(movable, choice)}
        solution, converged, note = _closest(cells, margins, cap, fixed=fixed)
        entry = {'ends': {margins[index]['id']: ('lower' if pick == 0 else 'upper')
                          for index, pick in zip(movable, choice)},
                 'feasible': solution is not None, 'converged': converged}
        if solution is not None:
            entry['mean'] = _overall_mean(cells, solution)
            means.append(entry['mean'])
        if note:
            entry['note'] = note
        entries.append(entry)
    return {'skipped': False, 'entries': entries,
            'mean_range': [min(means), max(means)] if means else None,
            'note': '把每个非退化边际固定到端点后重新求解；只展示端点的组合效应，不覆盖区间内部。'}


def analyze_snapshot(snapshot, settings=None):
    """在固定资料版本上运行分析，并把版本与摘要写进产物，避免读取时换成最新资料。"""
    result = analyze(snapshot['data'], settings)
    result['revision'] = snapshot['revision']
    result['data_digest'] = snapshot['digest']
    result['analysis_digest'] = hashlib.sha256(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                   allow_nan=False).encode()
    ).hexdigest()
    return result


def _choices(settings, margins, cells, excluded):
    return [
        {'topic': 'supported_cells', 'choice': 'only_observed_cells',
         'rationale': f'只使用样本中观察到的 {len(cells)} 个（地区、规模、行业）组合；'
                      f'{len(excluded)} 个过滤组合不补造。'},
        {'topic': 'point_estimate', 'choice': 'closest_to_design_weights',
         'rationale': '在全部可行解中取与既有设计权重欧氏距离最小者，避免无依据地偏向任一边际端点。'},
        {'topic': 'margins', 'choice': [margin['id'] for margin in margins],
         'rationale': '边际按资料给出的区间同时约束；交叠边际不拆成互斥分箱，也不求和当联合总体。'},
        {'topic': 'weight_cap', 'choice': settings['weight_cap'],
         'rationale': ('未设置单元格权重上限；需结合最大权重比与有效样本量判断稳健性。'
                       if settings['weight_cap'] is None else
                       '单元格校准总量不得超过既有设计权重的该倍数，防止单一组合撑起总体。')},
        {'topic': 'group_dependence', 'choice': 'cluster_bootstrap_by_group',
         'rationale': '同一集团内的企业共同回应，按集团整群重抽样，避免把集团内相关当成独立信息。'},
        {'topic': 'extrapolation', 'choice': 'selection_bias_assumption',
         'rationale': ASSUMPTION},
    ]


def analyze(data, settings=None):
    """返回完整校准分析；资料不被修改。"""
    settings = _settings(settings)
    rows = data['respondents']
    by_id = {margin['id']: margin for margin in data['margins']}
    if settings['margin_ids'] is None:
        margins = list(data['margins'])
    else:
        missing = [name for name in settings['margin_ids'] if name not in by_id]
        require(not missing, f'边际不存在: {missing}')
        margins = [by_id[name] for name in settings['margin_ids']]
    targets = settings['targets'] if settings['targets'] is not None else _default_targets(rows)
    require(margins, '校准至少需要一个边际；没有边际时样本无法约束总体规模。')
    cap = settings['weight_cap']

    cells = _cells(rows, settings['min_cell_respondents'])
    observed = {cell['key'] for cell in cells}
    all_keys = {tuple(row[field] for field in FIELDS) for row in rows}
    excluded = [{'key': list(key),
                 'respondents': sum(1 for row in rows if tuple(row[field] for field in FIELDS) == key)}
                for key in sorted(all_keys - observed)]
    levels = _levels(rows)
    expected = math.prod(len(levels[field]) for field in FIELDS)
    missing = [list(key) for key in itertools.product(*(levels[field] for field in FIELDS))
               if key not in observed]

    status, codes, message, details = _diagnose(cells, margins, cap, rows,
                                                settings['min_cell_respondents'])
    x = None
    converged = False
    solve_note = ''
    if status == 'ok':
        x, converged, solve_note = _closest(cells, margins, cap)
        if x is None:
            status, codes, message = 'conflict', ['margin_conflict'], solve_note or '约束下无可行解。'

    margin_report = []
    for margin in margins:
        matching = [cell for cell in cells if matches(cell['where'], margin['where'])]
        margin_report.append({
            'id': margin['id'], 'where': margin['where'],
            'lower': margin['lower'], 'upper': margin['upper'],
            'observed_cells': len(matching),
            'observed_respondents': sum(cell['respondents'] for cell in matching),
            'design_weight': sum(cell['design'] for cell in matching),
            'achieved': None if x is None else float(sum(x[index] for index, cell in enumerate(cells)
                                                         if matches(cell['where'], margin['where']))),
        })

    bootstrap_targets = [target for target in targets if _matching(cells, target)]
    bootstrap = _bootstrap(rows, margins, cap, bootstrap_targets,
                           settings['min_cell_respondents'],
                           settings['bootstrap_replicates'], settings['random_seed'])

    overall_target = {'id': 'all', 'where': {}}
    total_lower, total_upper = _total_bounds(cells, margins, cap) if cells else (None, None)
    identified = total_lower is not None and total_upper is not None and abs(total_upper - total_lower) <= 1e-6
    overall = None
    target_entries = []
    if x is not None:
        overall_range = {
            'mean': [_extreme(cells, margins, cap, overall_target, 'mean', False),
                     _extreme(cells, margins, cap, overall_target, 'mean', True)],
            'total': [_extreme(cells, margins, cap, overall_target, 'total', False),
                      _extreme(cells, margins, cap, overall_target, 'total', True)],
            'population': [total_lower, total_upper],
        }
        overall = _statistics(cells, x, overall_target)
        overall['identified_range'] = overall_range
        overall['bootstrap'] = bootstrap['intervals'].get('all') if bootstrap else None
        overall['status'], overall['codes'] = _assess(
            overall, settings, overall_range['mean'], overall['bootstrap'], bootstrap, identified)
        overall['assumption'] = ASSUMPTION

    for target in targets:
        entry = {'id': target['id'], 'where': target['where']}
        stats = _statistics(cells, x, target) if x is not None else None
        if stats is None:
            entry.update({'status': 'conflict', 'codes': ['no_support'] if status == 'ok' else codes,
                          'note': ('该人群在最小样本量过滤后没有任何观测单元格，不能估计；'
                                   '不合成无样本组合。' if status == 'ok' else
                                   '全局约束无解，未产生估计。')})
            target_entries.append(entry)
            continue
        entry.update(stats)
        entry['identified_range'] = {
            'mean': [_extreme(cells, margins, cap, target, 'mean', False),
                     _extreme(cells, margins, cap, target, 'mean', True)],
            'total': [_extreme(cells, margins, cap, target, 'total', False),
                      _extreme(cells, margins, cap, target, 'total', True)],
        }
        entry['bootstrap'] = bootstrap['intervals'].get(target['id']) if bootstrap else None
        entry['status'], entry['codes'] = _assess(
            stats, settings, entry['identified_range']['mean'], entry['bootstrap'], bootstrap, identified)
        entry['assumption'] = ASSUMPTION
        target_entries.append(entry)

    cap_entries, unrestricted = _cap_scenarios(cells, margins, cap) if cells else ([], None)
    relaxations = _margin_relaxations(cells, margins, cap) if cells else []
    if x is not None and unrestricted is not None:
        for entry in relaxations:
            if entry.get('mean') is not None:
                entry['delta_vs_requested'] = entry['mean'] - _overall_mean(cells, x)
    corners = _corners(cells, margins, cap, settings) if x is not None else {
        'skipped': True, 'entries': [], 'mean_range': None, 'note': '约束无解，未比较端点组合。'}
    minimal = _minimal_cap(cells, margins) if cells else None

    affected = [entry['id'] for entry in target_entries if entry['status'] == 'conflict']
    unstable = [entry['id'] for entry in target_entries if entry['status'] == 'unstable']
    extrapolated = [entry['id'] for entry in target_entries if entry['status'] == 'extrapolated']
    verdicts = [(entry['status'], entry['codes']) for entry in target_entries]
    if overall is not None:
        verdicts.append((overall['status'], overall['codes']))
    order = {'conflict': 0, 'unstable': 1, 'extrapolated': 2, 'calibrated': 3}
    if status != 'ok':
        final = status
        merged = list(codes)
    elif verdicts:
        final = min(verdicts, key=lambda item: order[item[0]])[0]
        merged = sorted({code for _, item_codes in verdicts for code in item_codes})
        if final == 'unstable' and not merged:
            merged = ['wide_identified_range']
    else:
        final = 'calibrated'
        merged = []
    if status != 'ok' and message:
        summary_message = message
    elif final == 'calibrated':
        summary_message = '在声明设置下，总体与各关注人群均通过有效信息量与区间检查。'
    else:
        summary_message = (f'总体可行，但结论分级为 {final}；受影响人群与原因代码见 '
                           f'unstable_targets / extrapolated_targets / conflict_targets 与各 target 的 codes。')

    return _plain({
        'meaning': MEANING,
        'assumption': ASSUMPTION,
        'settings': settings,
        'choices': _choices(settings, margins, cells, excluded),
        'data': {
            'respondents': len(rows),
            'groups': len({row['group'] for row in rows}),
            'cells': [{'key': list(cell['key']), 'respondents': cell['respondents'],
                       'groups': cell['groups'], 'design_weight': cell['design'],
                       'design_mean': cell['mean']} for cell in cells],
            'excluded_cells': excluded,
            'levels': levels,
            'coverage': {'observed_cells': len(cells), 'level_product': expected,
                         'missing_combinations': len(missing),
                         'missing_examples': missing[:20]},
        },
        'margins': margin_report,
        'diagnosis': {
            'status': final,
            'codes': merged,
            'message': summary_message,
            'conflict_targets': affected,
            'unstable_targets': unstable,
            'extrapolated_targets': extrapolated,
            'overall_status': overall['status'] if overall else None,
            'overall_codes': overall['codes'] if overall else None,
            'converged': converged,
            'solve_note': solve_note,
            **details,
        },
        'overall': overall,
        'targets': target_entries,
        'calibration': {
            'weight_cap': cap,
            'minimal_feasible_cap': minimal,
            'max_weight_ratio': (max(x[index] / cells[index]['design']
                                     for index in range(len(cells))) if x is not None else None),
            'bootstrap': bootstrap,
        },
        'tradeoffs': {
            'design_weighted_mean': (sum(row['base_weight'] * row['intention'] for row in rows)
                                     / sum(row['base_weight'] for row in rows)),
            'unrestricted_mean': unrestricted,
            'cap_scenarios': cap_entries,
            'margin_relaxations': relaxations,
            'margin_endpoint_corners': corners,
        },
    })
