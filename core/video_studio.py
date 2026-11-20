"""零额度本地成片：分镜文案 → 本地绘制画面 → 推拉/淡入 → 烧字幕 → 可选本地配音 → mp4。

边界：
- 画面全部由 PIL 本地绘制（渐变 + 几何 + 大字），不调用任何需要额度的生图接口；
  分镜里给了 image 路径就用本地已有素材，没有才自绘，所以“一定能拿到画面”。
- 配音用 Windows 自带 SAPI（本机有 Microsoft Huihui 中文音色），离线可用；
  没有中文音色或调用失败时自动降级为无声成片，不阻断出片。
- 合成用 imageio-ffmpeg 自带的 ffmpeg，不要求用户另装。
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from core.media_workflow import ffmpeg_path, inspect_video
from core import guga_paths

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = guga_paths.DELIVERY
WORK_DIR = guga_paths.RUNTIME / 'video-jobs'
FPS = 25
SIZES = {'1280x720': (1280, 720), '1920x1080': (1920, 1080)}
FONT_CANDIDATES = (r'C:\Windows\Fonts\msyhbd.ttc', r'C:\Windows\Fonts\msyh.ttc',
                   r'C:\Windows\Fonts\simhei.ttf', r'C:\Windows\Fonts\simsun.ttc')
PALETTES = (('#16324F', '#2B6DE5'), ('#132B3A', '#199C86'), ('#3A1D46', '#8E4EC6'),
            ('#123047', '#0E7C9E'), ('#2A2320', '#C9772E'), ('#1B2A41', '#47A3C9'))


class VideoError(ValueError):
    """分镜不可用或合成失败；message 直接给模型，方便它改分镜重试。"""


def _font(size):
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(draw, text, font, width):
    lines, line = [], ''
    for char in text:
        probe = line + char
        if draw.textlength(probe, font=font) <= width:
            line = probe
            continue
        lines.append(line)
        line = char
    if line:
        lines.append(line)
    return lines[:4]


def _gradient(size, top, bottom):
    width, height = size
    canvas = Image.new('RGB', (1, height))
    draw = ImageDraw.Draw(canvas)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(int(a + (b - a) * ratio) for a, b in zip(top, bottom))
        draw.point((0, y), fill=color)
    return canvas.resize(size)


def _rgb(value, fallback):
    text = str(value or '').strip().lstrip('#')
    if not re.fullmatch(r'[0-9a-fA-F]{6}', text):
        return fallback
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def _cover(image, size):
    width, height = size
    scale = max(width / image.width, height / image.height)
    resized = image.resize((max(width, int(image.width * scale)),
                            max(height, int(image.height * scale))), Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def render_frame(shot, index, accent, title, size, folder):
    """本地绘制一帧画面；有本地素材就用素材，没有就自绘，不依赖外部生图。"""
    width, height = size
    source = shot.get('image')
    if source and Path(str(source)).expanduser().is_file():
        frame = _cover(Image.open(str(source)).convert('RGB'), size)
        scrim = Image.new('RGBA', size, (8, 14, 24, 120))
        frame = Image.alpha_composite(frame.convert('RGBA'), scrim).convert('RGB')
    else:
        colors = PALETTES[index % len(PALETTES)]
        top, bottom = _rgb(colors[0], (22, 50, 79)), _rgb(colors[1], (43, 109, 229))
        frame = _gradient(size, top, bottom)
        glow = Image.new('RGBA', size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(glow)
        radius = int(height * .55)
        cx, cy = int(width * (.78 + .04 * math.sin(index))), int(height * .22)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     fill=tuple(accent) + (44,))
        glow = glow.filter(ImageFilter.GaussianBlur(radius // 3))
        frame = Image.alpha_composite(frame.convert('RGBA'), glow).convert('RGB')
        draw = ImageDraw.Draw(frame)
        for offset in range(3):
            y = int(height * (.72 + offset * .03))
            draw.line((int(width * .08), y, int(width * (.08 + .12 - offset * .03)), y),
                      fill=tuple(accent), width=max(2, height // 320))
    draw = ImageDraw.Draw(frame)
    accent_rgb = tuple(accent)
    draw.rectangle((0, 0, max(6, width // 180), height), fill=accent_rgb)
    number_font, head_font, tag_font = _font(int(height * .075)), _font(int(height * .1)), _font(int(height * .028))
    draw.text((int(width * .07), int(height * .09)), '%02d' % (index + 1), font=number_font, fill=accent_rgb)
    headline = str(shot.get('headline') or shot.get('visual') or '').strip()
    if headline:
        y = int(height * .35)
        for line in _wrap(draw, headline[:60], head_font, int(width * .8)):
            draw.text((int(width * .07), y), line, font=head_font, fill=(255, 255, 255))
            y += int(height * .12)
    if title:
        draw.text((int(width * .07), int(height * .86)), title[:32], font=tag_font,
                  fill=(255, 255, 255, 190))
    path = folder / ('frame-%02d.png' % index)
    frame.save(path)
    return path


def _srt_time(seconds):
    hours, rest = divmod(max(0.0, seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return '%02d:%02d:%02d,%03d' % (int(hours), int(minutes), int(secs), int(round((secs % 1) * 1000)))


def _write_srt(shots, folder):
    path = folder / 'captions.srt'
    lines, clock = [], 0.0
    for index, shot in enumerate(shots, 1):
        start, clock = clock, clock + shot['seconds']
        lines += [str(index), '%s --> %s' % (_srt_time(start), _srt_time(clock)),
                  str(shot['narration']), '']
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path


def write_subtitles(script, target, total=None):
    """把分镜写成 SRT；total 给定时按比例贴合外部成片的实际时长。"""
    shots, _ = _validate(script)
    if total:
        base = sum(shot['seconds'] for shot in shots) or 1.0
        scale = float(total) / base
        for shot in shots:
            shot['seconds'] = round(max(1.0, shot['seconds'] * scale), 2)
        drift = round(float(total) - sum(shot['seconds'] for shot in shots), 2)
        shots[-1]['seconds'] = round(max(1.0, shots[-1]['seconds'] + drift), 2)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines, clock = [], 0.0
    for index, shot in enumerate(shots, 1):
        start, clock = clock, clock + shot['seconds']
        lines += [str(index), '%s --> %s' % (_srt_time(start), _srt_time(clock)),
                  str(shot['narration']), '']
    target.write_text('\n'.join(lines), encoding='utf-8')
    return target


def _speak(text, target, rate=0):
    """Windows SAPI 离线配音；返回是否成功，失败不阻断出片。"""
    try:
        import win32com.client as win32
        voice = win32.Dispatch('SAPI.SpVoice')
        chosen = None
        for item in voice.GetVoices():
            if 'Chinese' in str(item.GetDescription()):
                chosen = item
                break
        if chosen:
            voice.Voice = chosen
        voice.Rate = rate
        stream = win32.Dispatch('SAPI.SpFileStream')
        stream.Open(str(target), 3)  # SSFMCreateForWrite
        voice.AudioOutputStream = stream
        voice.Speak(text)
        stream.Close()
        return target.is_file() and target.stat().st_size > 0
    except Exception:
        return False


def _run(command, folder, cwd=None):
    log = folder / ('ffmpeg-%d.log' % int(time.time() * 1000))
    with log.open('w', encoding='utf-8') as handle:
        run = subprocess.run(command, stdout=subprocess.PIPE, stderr=handle,
                             encoding='utf-8', errors='replace', timeout=600, cwd=cwd,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if run.returncode != 0:
        tail = log.read_text(encoding='utf-8', errors='replace')[-600:]
        raise VideoError('ffmpeg 合成失败：' + tail)
    return log


def _clip(frame, shot, index, out, size, folder):
    width, height = size
    frames = int(round(shot['seconds'] * FPS))
    if index % 2:
        zoom = "1+0.12*on/%d" % max(1, frames)
    else:
        zoom = "1.12-0.12*on/%d" % max(1, frames)
    fade_out = max(0.2, shot['seconds'] - 0.5)
    graph = ("zoompan=z='%s':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=%dx%d:fps=%d,"
             "fade=t=in:st=0:d=0.45,fade=t=out:st=%.2f:d=0.45,format=yuv420p"
             % (zoom, width, height, FPS, fade_out))
    _run([ffmpeg_path(), '-hide_banner', '-y', '-loop', '1', '-i', str(frame),
          '-t', '%.2f' % shot['seconds'], '-vf', graph, '-r', str(FPS),
          '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', str(out)], folder)


def _concat(clips, folder):
    listing = folder / 'clips.txt'
    listing.write_text(''.join("file '%s'\n" % c.name for c in clips), encoding='utf-8')
    joined = folder / 'joined.mp4'
    _run([ffmpeg_path(), '-hide_banner', '-y', '-f', 'concat', '-safe', '0',
          '-i', str(listing), '-c', 'copy', str(joined)], folder)
    return joined


def _voice_track(shots, folder, total):
    wavs = []
    for index, shot in enumerate(shots):
        target = folder / ('voice-%02d.wav' % index)
        if _speak(shot['narration'], target):
            wavs.append((target, shot['seconds']))
    if not wavs:
        return None
    parts, labels = [], []
    for index, (path, duration) in enumerate(wavs):
        parts += ['-i', str(path)]
        labels.append('[%d:a]atrim=0:%.2f,asetpts=PTS-STARTPTS,apad=whole_dur=%.2f[a%d]'
                      % (index, duration, duration, index))
    chain = ';'.join(labels) + ';' + ''.join('[a%d]' % i for i in range(len(wavs)))
    chain += 'concat=n=%d:v=0:a=1[a]' % len(wavs)
    chain += ';[a]apad=whole_dur=%.2f[out]' % total
    mixed = folder / 'voice.wav'
    _run([ffmpeg_path(), '-hide_banner', '-y'] + parts +
         ['-filter_complex', chain, '-map', '[out]', str(mixed)], folder)
    return mixed


def _validate(script):
    if not isinstance(script, dict):
        raise VideoError('脚本必须是对象：{title, theme:{accent}, shots:[{narration, visual, headline}]}')
    shots = script.get('shots')
    if not isinstance(shots, list) or not shots:
        raise VideoError('shots 不能为空，至少 1 镜；每镜给一句 narration')
    if len(shots) > 8:
        raise VideoError('单条最多 8 镜，请合并')
    clean = []
    for index, shot in enumerate(shots, 1):
        if not isinstance(shot, dict):
            raise VideoError('第 %d 镜必须是对象' % index)
        narration = str(shot.get('narration') or '').strip()
        if not narration:
            raise VideoError('第 %d 镜缺少 narration（要烧成字幕的那一句）' % index)
        if len(narration) > 80:
            raise VideoError('第 %d 镜字幕超过 80 字，请拆成两镜' % index)
        try:
            seconds = float(shot.get('seconds') or 0)
        except (TypeError, ValueError):
            seconds = 0
        clean.append(dict(narration=narration,
                          visual=str(shot.get('visual') or '').strip(),
                          headline=str(shot.get('headline') or '').strip(),
                          image=str(shot.get('image') or '').strip(),
                          seconds=seconds))
    total = sum(s['seconds'] for s in clean if s['seconds'] > 0)
    if total <= 0:
        total = 0
        for shot in clean:
            shot['seconds'] = 6.0
        total = 6.0 * len(clean)
    elif total < 20 or total > 40:
        scale = (28.0 if total < 20 else 34.0) / total
        for shot in clean:
            shot['seconds'] = round(max(3.0, min(12.0, shot['seconds'] * scale)), 2)
    return clean, script


def synthesize(script, voice=False, output=None, size='1280x720', source=None):
    """本地合成；返回 {ok,path,seconds,audio,subtitles,verified,timings}。"""
    size = SIZES.get(str(size or ''), SIZES['1280x720'])
    shots, meta = _validate(script)
    accent = _rgb((meta.get('theme') or {}).get('accent') if isinstance(meta.get('theme'), dict) else None,
                  (63, 169, 245))
    title = str(meta.get('title') or '').strip()
    folder = WORK_DIR / time.strftime('%Y%m%d-%H%M%S') / ('%03d' % (int(time.time() * 1000) % 1000))
    folder.mkdir(parents=True, exist_ok=True)
    timings, clock = {}, time.perf_counter()

    t0 = time.perf_counter()
    frames = [render_frame(shot, index, accent, title, size, folder) for index, shot in enumerate(shots)]
    timings['frames_ms'] = round((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    clips = []
    for index, (frame, shot) in enumerate(zip(frames, shots)):
        clip = folder / ('clip-%02d.mp4' % index)
        _clip(frame, shot, index, clip, size, folder)
        clips.append(clip)
    timings['clips_ms'] = round((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    joined = _concat(clips, folder)
    timings['concat_ms'] = round((time.perf_counter() - t0) * 1000)
    total = round(sum(s['seconds'] for s in shots), 2)

    srt = _write_srt(shots, folder)

    t0 = time.perf_counter()
    wav = _voice_track(shots, folder, total) if voice else None
    timings['voice_ms'] = round((time.perf_counter() - t0) * 1000)

    out = Path(output).expanduser() if output else OUTPUT_DIR / ('storyboard-' + time.strftime('%Y%m%d-%H%M%S') + '.mp4')
    if out.suffix.lower() != '.mp4':
        out = out.with_suffix('.mp4')
    out.parent.mkdir(parents=True, exist_ok=True)
    style = ("subtitles=%s:force_style='FontName=Microsoft YaHei,FontSize=18,"
             "PrimaryColour=&H00FFFFFF,OutlineColour=&H00101828,BorderStyle=1,"
             "Outline=1,Shadow=0,MarginV=26'" % srt.name)
    command = [ffmpeg_path(), '-hide_banner', '-y', '-i', str(joined)]
    if wav:
        command += ['-i', str(wav), '-map', '0:v', '-map', '1:a', '-c:a', 'aac', '-b:a', '128k', '-shortest']
    command += ['-vf', style, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21',
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(out)]
    t0 = time.perf_counter()
    _run(command, folder, cwd=folder)
    timings['burn_ms'] = round((time.perf_counter() - t0) * 1000)
    timings['total_ms'] = round((time.perf_counter() - clock) * 1000)
    result = inspect(out)
    result.update(ok=True, subtitles=str(srt), frames=len(shots), voice=bool(wav),
                  source_video=str(source) if source else '', timings=timings, folder=str(folder))
    return result


def inspect(path):
    path = Path(path).expanduser().resolve(strict=True)
    data = inspect_video(path)
    if data['seconds'] < 20 or data['seconds'] > 40.5:
        raise VideoError('成片时长 %.1f 秒不在 20—40 秒区间' % data['seconds'])
    return dict(path=str(path), seconds=data['seconds'], audio=data['audio'],
                size=data['size'], verified=True)


def script_of(value):
    """把 JSON 字符串/文件路径归一成脚本对象。"""
    if isinstance(value, dict):
        return value
    raw = str(value or '').strip()
    if not raw:
        raise VideoError('缺少 script')
    candidate = Path(raw).expanduser()
    if len(raw) < 400 and not raw.startswith('{') and candidate.is_file():
        raw = candidate.read_text(encoding='utf-8')
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise VideoError('script 不是合法 JSON：%s' % exc) from None
