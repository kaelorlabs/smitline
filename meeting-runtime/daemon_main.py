"""Production daemon entrypoint: loopback by default, server mode on request."""
from pathlib import Path
import argparse
import os
import secrets
import sys

from api_tokens import ApiTokenStore
from meeting_supervisor import ProductionMeetingSupervisor
from runtime_daemon import create_app, require_loopback_bind, require_server_bind
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
    parser = argparse.ArgumentParser(description='Colleague AI runtime daemon')
    parser.add_argument('--root', default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--runtime-root', default=None)
    parser.add_argument('--server', action='store_true',
                        help='allow a non-loopback bind; requires an API token')
    return parser


def build_call_service(project_root, runtime_root, data_root, daemon, lines=None):
    from call_hooks import DefaultCallHooks, load_hooks
    from call_notify import WebhookNotifier, load_or_create_secret
    from call_service import CallService
    from call_store import CallStore
    from meeting_line import MeetingLine

    store = CallStore(data_root / 'calls')
    notifier = WebhookNotifier(load_or_create_secret(data_root / 'webhook.secret'))
    hooks = load_hooks(os.environ.get('COLLEAGUE_CALL_HOOKS'), env_file=project_root / '.env',
                       store=store, notifier=notifier)
    workspace = runtime_root / 'codex-workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    available = {'meeting': MeetingLine(daemon, workspace=workspace)}
    available.update(lines or {})
    return CallService(store, hooks=hooks, lines=available)


def build_app(args):
    project_root = Path(args.root).resolve()
    data_root = daemon_data_path(project_root)
    api_tokens = ApiTokenStore(data_root / 'api-tokens.json')
    if args.server:
        host = require_server_bind(args.host, api_tokens)
    else:
        host = require_loopback_bind(args.host)
    runtime_root = Path(args.runtime_root or (project_root / 'meeting-runtime')).resolve()
    token, token_path = write_auth_token(project_root)
    def publish_auth_token(current_token):
        write_private_file(token_path, current_token + '\n')

    supervisor = ProductionMeetingSupervisor(project_root, runtime_root=runtime_root)
    app = create_app(
        root=data_root,
        bind_host=host,
        auth_token=token,
        on_auth_token=publish_auth_token,
        supervisor=supervisor,
        jobs_dir=runtime_root / 'jobs',
        visual_analyzer=CodexVisualAnalysisProvider(),
        call_service_factory=lambda daemon: build_call_service(
            project_root, runtime_root, data_root, daemon),
        api_tokens=api_tokens,
        server_mode=args.server,
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
