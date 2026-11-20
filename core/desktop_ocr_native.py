"""Memory-only Windows OCR via PyWinRT; no shell, policy change or upload."""
import asyncio
import io
import re
import time
from pathlib import Path
from PIL import Image


def readable_text(text):
    return re.sub(r'(?<=[\u3400-\u9fff，。！？：；、]) +(?=[\u3400-\u9fff，。！？：；、])','',str(text))


async def _recognize(image):
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
    from winrt.windows.storage.streams import DataWriter
    language=Language('zh-Hans-CN')
    engine=OcrEngine.try_create_from_language(language) if OcrEngine.is_language_supported(language) else OcrEngine.try_create_from_user_profile_languages()
    if engine is None:raise RuntimeError('系统尚未安装可用的 OCR 语言包。')
    limit=OcrEngine.max_image_dimension
    if max(image.size)>limit:
        image.thumbnail((limit,limit),Image.Resampling.LANCZOS)
    writer=DataWriter();bitmap=None
    try:
        writer.write_bytes(image.tobytes('raw','BGRA'))
        bitmap=SoftwareBitmap.create_copy_with_alpha_from_buffer(writer.detach_buffer(),BitmapPixelFormat.BGRA8,
            image.width,image.height,BitmapAlphaMode.IGNORE)
        result=await engine.recognize_async(bitmap)
        lines=[]
        for line in result.lines:
            lines.append(dict(text=line.text,words=[dict(text=word.text,x=word.bounding_rect.x,
                y=word.bounding_rect.y,w=word.bounding_rect.width,h=word.bounding_rect.height) for word in line.words]))
        return dict(text='\n'.join(readable_text(line['text']) for line in lines),lines=lines,language=engine.recognizer_language.language_tag)
    finally:
        if bitmap is not None:bitmap.close()
        writer.close()


def recognize(source,timeout=15):
    started=time.monotonic()
    try:
        if isinstance(source,Image.Image):image=source.convert('RGBA')
        else:
            with Image.open(io.BytesIO(source) if isinstance(source,bytes) else Path(source)) as picture:
                image=picture.convert('RGBA')
        async def run():return await asyncio.wait_for(_recognize(image),timeout=timeout)
        data=asyncio.run(run())
        return dict(ok=True,data=data,engine='windows_native',kind='none' if data['text'] else 'empty_text',
            elapsed_ms=round((time.monotonic()-started)*1000))
    except (ImportError,ModuleNotFoundError):
        return dict(ok=False,kind='dependency_missing',data={},message='缺少本地 OCR 组件，请安装 requirements-desktop-experience.txt 中的组件。')
    except TimeoutError:
        return dict(ok=False,kind='timeout',data={},message='桌面文字识别超时，请稍后重试。')
    except Exception as exc:
        return dict(ok=False,kind='recognition_failed',data={},message=str(exc)[:300])
