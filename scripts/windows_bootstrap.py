"""Prepare and start the native Windows dashboard from a clean download."""
import os
from pathlib import Path
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from scripts import macos_bootstrap as setup


def configure_data_environment(environ=None):
    env = os.environ if environ is None else environ
    data = Path(env.get('DOUBAO_DATA_DIR', '').strip() or ROOT).expanduser()
    config = Path(env.get('DOUBAO_CONFIG_PATH', '').strip() or data / 'config/config.yaml').expanduser()
    if not data.is_absolute() or not config.is_absolute():
        raise ValueError('DOUBAO_DATA_DIR 和 DOUBAO_CONFIG_PATH 必须为绝对路径')
    env.pop('DOUBAO_CONTAINER_PREVIEW', None)
    env['DOUBAO_CONFIG_PATH'] = str(config)
    return data, config


def start(log):
    from doubao_automation.config import load_config
    from doubao_automation.paths import DATA_ROOT
    import launch_dashboard
    setup.ensure_browser(load_config(), log)
    launch_dashboard.main(open_page=False)
    setup.verify_dashboard(launch_dashboard)
    if DATA_ROOT.resolve() == ROOT:
        subprocess.run(['cmd.exe', '/d', '/c', 'start_desktop_bridge.bat'],
                       cwd=ROOT, check=True, timeout=15)
    else:
        print('自定义数据目录不启用 Windows 桌面助手。', flush=True)
    launch_dashboard.open_browser()
    print('工作台已就绪：' + launch_dashboard.URL, flush=True)


def main():
    if sys.platform != 'win32' or sys.version_info < (3, 10):
        raise RuntimeError('此入口需要 Windows 和 Python 3.10 或更新版本。')
    data, config = configure_data_environment()
    log = setup.startup_log(data, 'windows_startup.log')
    print('启动日志：' + str(log), flush=True)
    try:
        if sys.argv[1:] == ['--start-dashboard']:
            start(log)
            return 0
        with setup.setup_lock(ROOT, name='windows_bootstrap'):
            if any(not setup.shutil.which(binary) for binary in ('ffmpeg', 'ffprobe')):
                raise RuntimeError('缺少 FFmpeg 或 FFprobe；可运行 winget install Gyan.FFmpeg，安装后重新打开终端。')
            python = setup.ensure_virtualenv(ROOT, log)
            setup.ensure_dependencies(ROOT, python, log)
            setup.ensure_private_config(ROOT, config)
            return subprocess.run([str(python), '-u', str(Path(__file__).resolve()), '--start-dashboard'],
                                  cwd=ROOT, timeout=420).returncode
    except Exception:
        with log.open('a', encoding='utf-8') as output:
            traceback.print_exc(file=output)
        raise


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('启动失败：' + str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1)
