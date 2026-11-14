from .common import finite, require, table, unique


def validate(data):
    rows = table(data, 'respondents')
    unique(rows)
    for row in rows:
        require(bool(row['group']), '集团标识不能为空')
        for field in ('region', 'size', 'sector'):
            require(isinstance(row[field], str) and row[field], '分层字段不能为空')
        require(finite(row['base_weight'], 'base_weight') > 0, '权重必须为正')
        finite(row['intention'], 'intention', 0)
    margins = table(data, 'margins')
    unique(margins)
    for margin in margins:
        require(isinstance(margin['where'], dict), '边际条件必须是对象')
        require(set(margin['where']) <= {'region', 'size', 'sector'}, '边际字段不支持')
        lower = finite(margin['lower'], 'lower', 0)
        upper = finite(margin['upper'], 'upper', 0)
        require(lower <= upper, '边际范围颠倒')


def matches(row, condition):
    return all(row.get(field) == value for field, value in condition.items())


def weighted_mean(rows):
    total = sum(row['base_weight'] for row in rows)
    return None if total == 0 else sum(row['intention'] * row['base_weight'] for row in rows) / total


def records(data):
    return data['respondents']


def summarize(data):
    rows = data['respondents']
    return {'respondents': len(rows), 'groups': len({row['group'] for row in rows}), 'design_weighted_mean': weighted_mean(rows), 'margin_support': [{'id': margin['id'], 'sample_count': sum(matches(row, margin['where']) for row in rows), 'design_weight_sum': sum(row['base_weight'] for row in rows if matches(row, margin['where']))} for margin in data['margins']], 'meaning': '既有设计权重描述，未作总体边际校准'}
