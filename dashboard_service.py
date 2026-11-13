"""仪表盘管理进程：单实例、监测代码更新并安全报告待重载。"""
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
if sys.platform == 'darwin':
    sys.dont_write_bytecode = True
from doubao_automation.paths import ROOT, DATA_ROOT, LOGS_DIR, TEMP_DIR

STATUS = TEMP_DIR / 'dashboard_service.json'
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MAX_FAST_FAILURES = 3
STABLE_WORKER_SECONDS = 30
RELOAD_WAIT_SECONDS = 8


def local_port_in_use():
    """Do not start a competing worker when a local listener already exists."""
    try:
        with socket.create_connection(('127.0.0.1', 8782), timeout=1):
            return True
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        raise RuntimeError('无法确认本地端口状态，已取消启动') from exc


def source_files():
    return [ROOT/'dashboard.py', ROOT/'run_pipeline.py', *sorted((ROOT/'doubao_automation').glob('*.py'))]


def revision():
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in source_files())


def validate_sources():
    for path in source_files():
        compile(path.read_bytes(), str(path), 'exec')


def worker_state():
    try:
        with LOCAL_HTTP.open('http://127.0.0.1:8782/api/state',timeout=2) as r:
            state=json.load(r)
            if (not state.get('project_root') or not state.get('data_root')
                    or Path(state['project_root']).resolve()!=ROOT.resolve()
                    or Path(state['data_root']).resolve()!=DATA_ROOT.resolve()):
                return None
            return state
    except Exception:
        return None


def publish(worker, **fields):
    data={'service_pid':os.getpid(), 'worker_pid':worker.pid if worker else None,
          'updated':time.time(), **fields}
    tmp=STATUS.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    os.replace(tmp,STATUS)


def start_worker(reload_token=None):
    env=os.environ.copy()
    env['DOUBAO_DASHBOARD_SERVICE_PID']=str(os.getpid())
    env['PYTHONIOENCODING']='utf-8'
    env['PYTHONDONTWRITEBYTECODE']='1'
    if reload_token:
        env['DOUBAO_DASHBOARD_RELOAD_TOKEN']=reload_token
    with (LOGS_DIR/'dashboard.log').open('a',encoding='utf-8') as log:
        return subprocess.Popen([sys.executable,'-u',str(ROOT/'dashboard.py')],cwd=ROOT,
                                env=env,stdout=log,stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def request_worker_reload(worker,token):
    """Close the run gate only after verifying this idle service-owned worker."""
    state=worker_state()
    if (not state or state.get('server_pid')!=worker.pid
            or state.get('service_pid')!=os.getpid()):
        return False,'无法确认工作进程归属，保持当前服务运行'
    if (state.get('running') or state.get('login_windows_open')
            or state.get('postprocessing') or state.get('shutdown_pending')):
        return False,'工作台仍有运行中的任务或窗口，等待空闲后重载'
    req=urllib.request.Request('http://127.0.0.1:8782/api/service/prepare-reload',data=b'{}',
        headers={'Host':'127.0.0.1:8782','Content-Type':'application/json',
                 'X-Dashboard-Reload-Token':token},method='POST')
    try:
        with LOCAL_HTTP.open(req,timeout=3) as response:
            result=json.load(response)
        if result.get('ok') and result.get('server_pid')==worker.pid:
            return True,''
        return False,str(result.get('error') or '工作进程拒绝安全重载')
    except Exception as exc:
        return False,'安全重载握手失败：'+type(exc).__name__


def restart_idle_worker(worker,token):
    ready,error=request_worker_reload(worker,token)
    if not ready:
        return None,error
    worker.terminate()
    try:
        worker.wait(timeout=RELOAD_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        worker.kill()
        try:worker.wait(timeout=RELOAD_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            return None,'空闲工作进程在安全停止期限内未退出'
    if worker.poll() is None:
        return None,'空闲工作进程仍在运行，未启动替代进程'
    if local_port_in_use():
        return None,'工作进程已退出但本地端口仍被占用，未关闭其他进程'
    return start_worker(token),''


def main():
    TEMP_DIR.mkdir(parents=True,exist_ok=True)
    LOGS_DIR.mkdir(parents=True,exist_ok=True)
    with (TEMP_DIR/'dashboard_service.lock').open('a+b') as singleton:
        singleton.seek(0,os.SEEK_END)
        if not singleton.tell():singleton.write(b'0');singleton.flush()
        singleton.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(singleton.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(singleton.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            print('仪表盘管理进程已经运行，复用现有服务。',flush=True)
            return
        worker=None
        reload_token=secrets.token_urlsafe(32)
        try:
            validate_sources()
            current=revision()
            if local_port_in_use():
                print('本机 8782 端口已有服务；为避免冲突，管理进程不启动或关闭任何工作进程。',flush=True)
                return
            worker=start_worker(reload_token)
            worker_started_at=time.monotonic()
            fast_failures=0
            pending_revision=None
            publish(worker,reload_pending=False,error=None)
            while True:
                time.sleep(1)
                if worker.poll() is not None:
                    if time.monotonic()-worker_started_at < STABLE_WORKER_SECONDS:
                        fast_failures+=1
                    else:
                        fast_failures=0
                    if fast_failures >= MAX_FAST_FAILURES:
                        message='工作进程连续快速退出，已停止重启；请检查端口和仪表盘日志'
                        publish(worker,reload_pending=False,error=message)
                        print(message,flush=True)
                        break
                    publish(worker,reload_pending=True,error=f'工作进程退出：{worker.returncode}，正在恢复')
                    time.sleep(1)
                    try:
                        validate_sources()
                        if local_port_in_use():
                            message='本机 8782 端口已被占用，已停止重启；不会关闭该服务'
                            publish(worker,reload_pending=False,error=message)
                            print(message,flush=True)
                            break
                        worker=start_worker(reload_token);worker_started_at=time.monotonic();current=revision();pending_revision=None
                        publish(worker,reload_pending=False,error=None)
                    except (SyntaxError,OSError,RuntimeError) as exc:
                        publish(worker,reload_pending=False,error=str(exc))
                        print('无法安全确认端口或恢复工作进程，已停止重启：'+str(exc),flush=True)
                        break
                    continue
                try:latest=revision()
                except OSError:continue
                if latest==current:
                    if pending_revision is not None:
                        pending_revision=None
                        publish(worker,reload_pending=False,error=None)
                    continue
                if latest==pending_revision:
                    try:
                        replacement,error=restart_idle_worker(worker,reload_token)
                        if replacement is None:
                            publish(worker,reload_pending=True,error=error)
                        else:
                            worker=replacement;worker_started_at=time.monotonic();fast_failures=0
                            current=revision();pending_revision=None
                            publish(worker,reload_pending=False,error=None)
                            print('工作进程已在空闲状态安全重载。',flush=True)
                    except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
                        publish(worker,reload_pending=True,error='安全重载失败：'+str(exc))
                        print('安全重载失败：'+str(exc),flush=True)
                    continue
                pending_revision=latest
                try:
                    validate_sources()
                except (SyntaxError,OSError) as exc:
                    publish(worker,reload_pending=True,error=str(exc))
                    continue
                message='检测到代码更新；等待确认工作台空闲后安全重载'
                publish(worker,reload_pending=True,error=message)
                print(message,flush=True)
        finally:
            if worker and worker.poll() is None:
                print('管理进程退出；工作进程可能仍在运行，未强制终止。',flush=True)
            singleton.seek(0)
            if os.name == 'nt':
                msvcrt.locking(singleton.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(singleton.fileno(),fcntl.LOCK_UN)


if __name__=='__main__':main()
