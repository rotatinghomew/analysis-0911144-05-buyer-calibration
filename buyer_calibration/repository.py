import hashlib
import json
from pathlib import Path
import sqlite3

from .domain import validate


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Conflict(ValueError):
    pass


class Repository:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS revisions(
                    revision INTEGER PRIMARY KEY AUTOINCREMENT,
                    digest TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts(
                    request_key TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    revision INTEGER NOT NULL REFERENCES revisions(revision)
                );
                CREATE TABLE IF NOT EXISTS analyses(
                    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_key TEXT NOT NULL UNIQUE,
                    digest TEXT NOT NULL,
                    revision INTEGER NOT NULL REFERENCES revisions(revision),
                    settings_digest TEXT NOT NULL,
                    settings TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
            ''')

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def import_data(self, data, request_key):
        if not isinstance(request_key, str) or not request_key or len(request_key) > 200:
            raise ValueError('request_key 必须是长度不超过 200 的非空字符串')
        if not isinstance(data, dict):
            raise ValueError('资料必须是对象')
        try:
            validate(data)
        except (KeyError, TypeError, IndexError) as error:
            raise ValueError('资料结构不完整或字段类型错误') from error
        payload = canonical(data)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = connection.execute('SELECT digest, revision FROM receipts WHERE request_key=?', (request_key,)).fetchone()
            if receipt:
                if receipt[0] != digest:
                    raise Conflict('相同请求号不能导入不同资料')
                return {'revision': receipt[1], 'digest': digest, 'replayed': True}
            connection.execute('INSERT OR IGNORE INTO revisions(digest,payload) VALUES (?,?)', (digest, payload))
            revision = connection.execute('SELECT revision FROM revisions WHERE digest=?', (digest,)).fetchone()[0]
            connection.execute('INSERT INTO receipts VALUES(?,?,?)', (request_key, digest, revision))
        return {'revision': revision, 'digest': digest, 'replayed': False}

    def get(self, revision=None):
        with self.connect() as connection:
            if revision is None:
                row = connection.execute('SELECT revision,digest,payload FROM revisions ORDER BY revision DESC LIMIT 1').fetchone()
            else:
                row = connection.execute('SELECT revision,digest,payload FROM revisions WHERE revision=?', (revision,)).fetchone()
        if row is None:
            raise LookupError('资料版本不存在')
        return {'revision': row[0], 'digest': row[1], 'data': json.loads(row[2])}

    def history(self):
        with self.connect() as connection:
            return [{'revision': row[0], 'digest': row[1]} for row in connection.execute('SELECT revision,digest FROM revisions ORDER BY revision')]

    def save_analysis(self, analysis, request_key, settings=None):
        """保存一次分析产物，绑定当时读取的资料版本；重复请求号只允许同一份产物。"""
        if not isinstance(request_key, str) or not request_key or len(request_key) > 200:
            raise ValueError('request_key 必须是长度不超过 200 的非空字符串')
        if not isinstance(analysis, dict):
            raise ValueError('分析结果必须是对象')
        data_digest = analysis.get('data_digest')
        revision = analysis.get('revision')
        if not isinstance(data_digest, str) or not isinstance(revision, int):
            raise ValueError('分析结果缺少资料版本或摘要')
        try:
            payload = canonical(analysis)
            settings_text = canonical(settings or {})
        except (TypeError, ValueError) as error:
            raise ValueError('分析结果不能序列化') from error
        digest = hashlib.sha256(payload.encode()).hexdigest()
        settings_digest = hashlib.sha256(settings_text.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            stored = connection.execute(
                'SELECT revision,digest,settings_digest FROM analyses WHERE request_key=?',
                (request_key,),
            ).fetchone()
            if stored:
                if stored[1] != digest:
                    raise Conflict('相同请求号不能保存不同分析结果')
                return {'analysis_id': connection.execute(
                    'SELECT analysis_id FROM analyses WHERE request_key=?', (request_key,)).fetchone()[0],
                    'revision': stored[0], 'digest': digest, 'settings_digest': settings_digest,
                    'replayed': True}
            known = connection.execute('SELECT digest FROM revisions WHERE revision=?', (revision,)).fetchone()
            if known is None:
                raise LookupError('资料版本不存在')
            if known[0] != data_digest:
                raise Conflict('分析声明的资料摘要与该版本不符')
            cursor = connection.execute(
                'INSERT INTO analyses(request_key,digest,revision,settings_digest,settings,payload) VALUES(?,?,?,?,?,?)',
                (request_key, digest, revision, settings_digest, settings_text, payload),
            )
            analysis_id = cursor.lastrowid
        return {'analysis_id': analysis_id, 'revision': revision, 'digest': digest,
                'settings_digest': settings_digest, 'replayed': False}

    def analyses(self, revision=None):
        with self.connect() as connection:
            if revision is None:
                rows = connection.execute(
                    'SELECT analysis_id,revision,digest,settings_digest FROM analyses ORDER BY analysis_id'
                ).fetchall()
            else:
                rows = connection.execute(
                    'SELECT analysis_id,revision,digest,settings_digest FROM analyses WHERE revision=? ORDER BY analysis_id',
                    (revision,),
                ).fetchall()
        return [{'analysis_id': row[0], 'revision': row[1], 'digest': row[2],
                 'settings_digest': row[3]} for row in rows]

    def analysis(self, analysis_id):
        with self.connect() as connection:
            row = connection.execute(
                'SELECT analysis_id,revision,digest,settings_digest,settings,payload FROM analyses WHERE analysis_id=?',
                (analysis_id,),
            ).fetchone()
        if row is None:
            raise LookupError('分析记录不存在')
        return {'analysis_id': row[0], 'revision': row[1], 'digest': row[2],
                'settings_digest': row[3], 'settings': json.loads(row[4]),
                'result': json.loads(row[5])}
