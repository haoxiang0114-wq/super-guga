"""Normalize local speech output to the user's Simplified Chinese UI language."""
import os
import re


def normalize_transcript(text):
    text=re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])','',str(text or '').strip())
    if not text or os.name!='nt':return text
    import ctypes
    fn=ctypes.WinDLL('kernel32',use_last_error=True).LCMapStringEx
    fn.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_wchar_p,ctypes.c_int,
        ctypes.c_wchar_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ssize_t]
    fn.restype=ctypes.c_int
    count=fn('zh-CN',0x02000000,text,len(text),None,0,None,None,0)
    if count<=0:return text
    output=ctypes.create_unicode_buffer(count+1)
    return output.value.replace('计画','计划') if fn('zh-CN',0x02000000,text,len(text),output,count,None,None,0)>0 else text
