# -*- coding: utf-8 -*-
"""跨平台 HTML 剪贴板读取的无界面适配。"""

import re
import subprocess

def read_clipboard_html_macos():
    """读取 macOS 剪贴板中的 HTML 富文本。"""
    script = (
        'ObjC.import("AppKit");'
        '(function(){var pb=$.NSPasteboard.generalPasteboard;'
        'var types=["public.html","Apple HTML pasteboard type","HTML Format"];'
        'for(var i=0;i<types.length;i++){var v=pb.stringForType(types[i]);'
        'if(v){return ObjC.unwrap(v);}}return "";})()'
    )
    try:
        result = subprocess.run(["osascript", "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, errors="ignore")
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except Exception:
        pass
    try:
        result = subprocess.run(["pbpaste", "-Prefer", "html"],
                                capture_output=True, text=True, errors="ignore")
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return ""


def read_clipboard_html_windows():
    """读取 Windows 剪贴板中的 CF_HTML 富文本。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.RegisterClipboardFormatW.restype = ctypes.c_uint
        user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]

        cf_html = user32.RegisterClipboardFormatW("HTML Format")
        if not cf_html or not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(cf_html)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                data = ctypes.string_at(pointer, kernel32.GlobalSize(handle))
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
        match = re.search(rb"StartHTML:(\d+)", data)
        if match:
            start = int(match.group(1))
            if 0 <= start < len(data):
                return data[start:].decode("utf-8", errors="replace")
        index = data.find(b"<")
        return data[index:].decode("utf-8", errors="replace") if index >= 0 else data.decode("utf-8", errors="replace")
    except Exception:
        return ""
