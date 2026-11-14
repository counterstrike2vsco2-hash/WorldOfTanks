# -- coding: utf-8 --
import functools
import traceback
import platform
from gui.mods.offhangar.utils import doLog as _doLog

doLog = functools.partial(_doLog, 'OFFHANGAR')
LOG_NOTE = functools.partial(doLog, '[NOTE]')
LOG_DEBUG = functools.partial(doLog, '[DEBUG]')
LOG_ERROR = functools.partial(doLog, '[ERROR]')


def _getGPUInfo():
    try:
        import BigWorld
        if hasattr(BigWorld, 'device'):
            return str(BigWorld.device())
    except Exception:
        pass
    for attr in ('wg_getVideoCardName', 'videoCardName', 'wg_getGPUName'):
        try:
            import BigWorld
            fn = getattr(BigWorld, attr, None)
            if fn is not None:
                return str(fn())
        except Exception:
            pass
    return 'unknown GPU'


def _getCPUInfo():
    try:
        return platform.processor() or 'unknown CPU'
    except Exception:
        return 'unknown CPU'


def _getRAMInfo():
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ('dwLength', ctypes.c_ulong),
                ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('sullAvailExtendedVirtual', ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        totalGB = stat.ullTotalPhys / (1024.0 ** 3)
        return '%.1f GB' % totalGB
    except Exception:
        return 'unknown RAM'


def _logHeader():
    try:
        import time
        doLog('=' * 80)
        timeStr = time.strftime('%Y-%m-%d %H:%M:%S')
        gpu = _getGPUInfo()
        cpu = _getCPUInfo()
        ram = _getRAMInfo()
        doLog('[SYSINFO]', '%s | GPU: %s | CPU: %s | RAM: %s' % (timeStr, gpu, cpu, ram))
    except Exception:
        try:
            doLog('=' * 80)
            doLog('[SYSINFO]', 'unavailable')
        except Exception:
            pass


def _redirectPythonLog():
    try:
        import debug_utils as du
        _orig = du.LOG_CURRENT_EXCEPTION
        def _patched(*args, **kwargs):
            _orig(*args, **kwargs)
            try:
                tb = traceback.format_exc()
                if tb and tb.strip() != 'NoneType: None':
                    doLog('[PY-EXCEPTION]', tb)
            except Exception:
                pass
        du.LOG_CURRENT_EXCEPTION = _patched
        doLog('[NOTE]', 'python.log redirection installed')
    except Exception:
        pass
    
    try:
        import BigWorld
        if hasattr(BigWorld, 'wg_writeToLog'):
            _orig = BigWorld.wg_writeToLog

            def _patched(msg):
                _orig(msg)
                try:
                    s = str(msg)
                    if '[OFFHANGAR]' not in s:
                        doLog('[PY]', s)
                except Exception:
                    pass

            BigWorld.wg_writeToLog = _patched
            doLog('[NOTE]', 'python.log redirection installed (wg_writeToLog)')
            return
    except Exception:
        pass

    # Фолбэк: патчим debug_utils, если wg_writeToLog недоступен
    try:
        import debug_utils as du
        for name in dir(du):
            if name.startswith('LOG_') and callable(getattr(du, name)):
                orig = getattr(du, name)

                def _make(orig=orig, tag=name[4:]):
                    def _p(*args, **kwargs):
                        orig(*args, **kwargs)
                        try:
                            doLog('[PY-%s]' % tag, *args)
                        except Exception:
                            pass
                    return _p

                setattr(du, name, _make())

        doLog('[NOTE]', 'python.log redirection installed (debug_utils fallback)')
    except Exception:
        pass


_logHeader()
_redirectPythonLog()