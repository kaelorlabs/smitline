"""Loopback production daemon entrypoint."""
from pathlib import Path
import argparse
import secrets
import sys

from meeting_supervisor import ProductionMeetingSupervisor
from runtime_daemon import create_app, require_loopback_bind
from runtime_state import daemon_data_path, daemon_token_path, write_private_file
from visual_analysis import CodexVisualAnalysisProvider


def write_auth_token(root, token=None):
    token = secrets.token_urlsafe(32) if token is None else token
    if not isinstance(token, str) or not token:
        raise ValueError('auth token must be a non-empty string')
    path = daemon_token_path(root)
    write_private_file(path, token + '\n')
    return token, path


def build_parser():
    parser = argparse.ArgumentParser(description='Colleague AI local runtime daemon')
    parser.add_argument('--root', default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--runtime-root', default=None)
    return parser


def build_app(args):
    host = require_loopback_bind(args.host)
    project_root = Path(args.root).resolve()
    runtime_root = Path(args.runtime_root or (project_root / 'meeting-runtime')).resolve()
    token, token_path = write_auth_token(project_root)
    supervisor = ProductionMeetingSupervisor(project_root, runtime_root=runtime_root)
    app = create_app(
        root=daemon_data_path(project_root),
        bind_host=host,
        auth_token=token,
        supervisor=supervisor,
        jobs_dir=runtime_root / 'jobs',
        visual_analyzer=CodexVisualAnalysisProvider(),
    )
    supervisor.bind_daemon(app.runtime_daemon)
    app.meeting_supervisor = supervisor
    app.auth_token_path = token_path

    async def on_startup(_app):
        await supervisor.reconcile(app.runtime_daemon)

    async def on_cleanup(_app):
        await supervisor.shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app, host, args.port, token_path


def main(argv=None):
    args = build_parser().parse_args(argv)
    app, host, port, token_path = build_app(args)
    print(f'daemon listening on {host}:{port}', flush=True)
    print(f'auth token file: {token_path}', flush=True)
    from aiohttp import web
    web.run_app(app, host=host, port=port, print=None)
    return 0


if __name__ == '__main__':
    sys.exit(main())
