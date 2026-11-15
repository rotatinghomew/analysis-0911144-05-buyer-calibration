import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from .calibration import analyze_snapshot
from .client import Client
from .repository import Repository
from .service import make_server


def main():
    parser = argparse.ArgumentParser(description='本地资料导入与业务概览')
    subcommands = parser.add_subparsers(dest='command', required=True)
    server_parser = subcommands.add_parser('serve', help='启动资料接口')
    server_parser.add_argument('--database', default='/tmp/analysis-data.sqlite3')
    server_parser.add_argument('--host', default='127.0.0.1')
    server_parser.add_argument('--port', type=int, default=8080)
    server_parser.add_argument('--load', type=Path)
    demo_parser = subcommands.add_parser('demo', help='运行本地资料往返演示')
    demo_parser.add_argument('--fixture', type=Path, default=Path('fixtures/demo.json'))
    calibrate_parser = subcommands.add_parser('calibrate', help='对固定版本运行校准分析并保存')
    calibrate_parser.add_argument('--database', default='/tmp/analysis-data.sqlite3')
    calibrate_parser.add_argument('--revision', type=int)
    calibrate_parser.add_argument('--settings', type=Path, help='JSON 设置文件')
    calibrate_parser.add_argument('--request-key', help='保存分析时使用的请求号')
    args = parser.parse_args()
    if args.command == 'serve':
        repository = Repository(args.database)
        if args.load:
            repository.import_data(json.loads(args.load.read_text()), 'startup')
        server = make_server(repository, args.host, args.port)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    elif args.command == 'calibrate':
        repository = Repository(args.database)
        settings = json.loads(args.settings.read_text()) if args.settings else None
        snapshot = repository.get(args.revision)
        analysis = analyze_snapshot(snapshot, settings)
        output = {'revision': analysis['revision'], 'data_digest': analysis['data_digest'],
                  'analysis_digest': analysis['analysis_digest'],
                  'diagnosis': analysis['diagnosis']}
        if args.request_key:
            output['saved'] = repository.save_analysis(analysis, args.request_key, settings)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        with TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / 'data.sqlite3')
            server = make_server(repository)
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                client = Client(f'http://127.0.0.1:{server.server_port}')
                data = json.loads(args.fixture.read_text())
                receipt = client.import_data(data, 'demo')
                summary = client.summary(receipt['revision'])
                calibration = client.calibration(receipt['revision'], {'bootstrap_replicates': 20})
                print(json.dumps({'import': receipt, 'summary': summary,
                                  'calibration': calibration}, ensure_ascii=False, indent=2))
            finally:
                server.shutdown()
                worker.join()
                server.server_close()


if __name__ == '__main__':
    main()
