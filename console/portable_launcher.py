"""Persistent desktop entry for the existing reading site (stdlib only)."""
from __future__ import annotations

import argparse
import functools
import hmac
import http.server
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

APP = Path(__file__).resolve().parent
STATE = APP / 'data'
IDENTITY = 'look-tongji-original-reader-v1'


def site_directory():
    config = APP / 'data/config.json'
    root = Path(json.loads(config.read_text(encoding='utf-8-sig'))['workspace_root'])
    site = root / 'site'
    return site.resolve()


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def healthy(record, site):
    try:
        port = int(record['port'])
        if not 1 <= port <= 65535:
            return False
        # Ignore proxy settings: health checks always stay on loopback.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f'http://127.0.0.1:{port}/__desktop_health__', headers={'X-Console-Token': record['token']})
        with opener.open(request, timeout=1) as response:
            health = json.load(response)
        return health == {
            'app': IDENTITY, 'token': record['token'],
            'site': str(site), 'pid': record['pid'],
        }
    except (OSError, ValueError, KeyError, TypeError):
        return False


def serve(site, token, manager=False):
    if manager:
        sys.path.insert(0, str(APP / 'project'))
        from console.server import make_server
        server = make_server('127.0.0.1', 0, token)
        base_handler = server.RequestHandlerClass

        class ManagerHandler(base_handler):
            def do_GET(self):
                if self.path != '/__desktop_health__':
                    return super().do_GET()
                if not self._origin_ok() or not hmac.compare_digest(self.headers.get('X-Console-Token', ''), token):
                    return self._send_json({'error': 'forbidden'}, 403)
                self._send_json({'app': IDENTITY, 'token': token,
                                 'site': str(site), 'pid': os.getpid()})

        server.RequestHandlerClass = ManagerHandler
        with server:
            atomic_json(STATE / f'ready-{token}.json', {
                'port': server.server_port, 'token': token, 'pid': os.getpid(),
            })
            server.serve_forever()
        return
    spec = importlib.util.spec_from_file_location('original_reader', APP / 'original_serve.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Handler(module._QuietHandler):
        def do_GET(self):
            if self.path == '/__desktop_health__':
                body = json.dumps({'app': IDENTITY, 'token': token,
                                   'site': str(site), 'pid': os.getpid()}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()

    class Server(http.server.ThreadingHTTPServer):
        allow_reuse_address = False

        def server_bind(self):
            # Windows SO_REUSEADDR can allow two processes to bind one port.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    handler = functools.partial(Handler, directory=str(site))
    try:
        server = Server(('127.0.0.1', 1109), handler)
    except OSError:
        server = Server(('127.0.0.1', 0), handler)
    with server:
        atomic_json(STATE / f'ready-{token}.json', {
            'port': server.server_port, 'token': token, 'pid': os.getpid(),
        })
        server.serve_forever()


def launch(site, manager=False):
    import msvcrt
    STATE.mkdir(parents=True, exist_ok=True)
    # OS releases the lock if a launcher crashes; no stale lockfile deadlock.
    with (STATE / 'launch.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        deadline = time.monotonic() + 35
        while True:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('启动等待超时，请稍后再试。')
                time.sleep(.1)
        try:
            if not manager and not (site / 'index.html').is_file():
                sys.path.insert(0, str(APP / 'project/scripts'))
                from tongji_backend.workspace import load_workspace_config, build_workspace_wiki
                build_workspace_wiki(load_workspace_config())
            record_path = STATE / ('manager-service.json' if manager else 'service.json')
            try:
                record = json.loads(record_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                record = {}
            if healthy(record, site):
                return record, True
            token = secrets.token_hex(16)
            ready = STATE / f'ready-{token}.json'
            env = os.environ.copy()
            env['PYTHONHOME'] = str(APP / 'runtime')
            env['PYTHONNOUSERSITE'] = '1'
            env.pop('PYTHONPATH', None)
            env['PLAYWRIGHT_BROWSERS_PATH'] = str(APP / 'browsers')
            if (APP / 'bin/ffmpeg.exe').is_file():
                env['LOOK_TONGJI_FFMPEG'] = str(APP / 'bin/ffmpeg.exe')
            with (STATE / 'server.log').open('ab') as log:
                command = [sys.executable, str(Path(__file__).resolve()), '--serve',
                           '--site', str(site), '--token', token]
                if manager:
                    command.append('--manager')
                process = subprocess.Popen(
                    command, cwd=APP,
                    env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if ready.exists():
                        record = json.loads(ready.read_text(encoding='utf-8'))
                        if healthy(record, site):
                            atomic_json(record_path, record)
                            return record, False
                    if process.poll() is not None:
                        raise RuntimeError('阅读服务启动失败，请查看应用/state/server.log。')
                    time.sleep(.1)
                raise RuntimeError('阅读服务启动超时，请查看应用/state/server.log。')
            except Exception:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                raise
            finally:
                ready.unlink(missing_ok=True)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def prepare_environment():
    STATE.mkdir(parents=True, exist_ok=True)
    os.environ['LOOK_TONGJI_CONFIG_PATH'] = str(STATE / 'config.json')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(APP / 'browsers')
    os.environ['PYTHONHOME'] = str(APP / 'runtime')
    os.environ['PYTHONNOUSERSITE'] = '1'
    os.environ.pop('PYTHONPATH', None)
    if not (STATE / 'config.json').exists():
        temporary = STATE / ('config-' + secrets.token_hex(8) + '.tmp')
        temporary.write_text(json.dumps({'workspace_root': str(APP.parent / '课程资料'),
                             'owner_name': '我', 'site_name': '我的课程知识库'},
                             ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            # On Windows rename refuses an existing target: preserve user settings.
            os.rename(temporary, STATE / 'config.json')
        except FileExistsError:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--manager', action='store_true')
    parser.add_argument('--site')
    parser.add_argument('--token')
    args = parser.parse_args()
    try:
        prepare_environment()
        if args.serve:
            serve(Path(args.site), args.token, args.manager)
            return
        record, reused = launch(APP / 'project' if args.manager else site_directory(), args.manager)
        (STATE / 'last-error.txt').unlink(missing_ok=True)
        url = f"http://127.0.0.1:{record['port']}/"
        if args.manager:
            url += '?token=' + record['token']
        if args.no_open:
            print(json.dumps({'url': url, 'pid': record['pid'], 'reused': reused}))
        elif not webbrowser.open(url):
            raise RuntimeError(f'无法打开默认浏览器，请手动访问 {url}')
    except Exception as error:
        STATE.mkdir(parents=True, exist_ok=True)
        message = str(error)
        (STATE / 'last-error.txt').write_text(message, encoding='utf-8')
        if not args.no_open and not args.serve:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, '课程助手启动失败', 16)
        if sys.stderr:
            print(message, file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
