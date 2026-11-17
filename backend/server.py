import os
import subprocess
import json
import shutil
import datetime
import time
import re
from flask import Flask, request, send_file, jsonify
from youtube_transcript_api import YouTubeTranscriptApi

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, '../frontend'))
UPLOAD_FOLDER = os.path.abspath(os.path.join(BASE_DIR, '../upload'))
TEMP_FOLDER = os.path.join(UPLOAD_FOLDER, 'temp')
PROJECTS_FOLDER = os.path.join(UPLOAD_FOLDER, 'projects')
TRIMMED_DIR = os.path.join(UPLOAD_FOLDER, 'trimmed')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)
os.makedirs(PROJECTS_FOLDER, exist_ok=True)
os.makedirs(TRIMMED_DIR, exist_ok=True)

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path='/')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB for long multi-scene videos


@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response


SUCCESS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <style>
        body {{ margin: 0; color: white; font-family: Arial; text-align: center; padding-top: 30px; overflow-x: hidden; background: #000; }}
        #bg-video {{ position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -10; object-fit: cover; filter: brightness(0.35); }}
        .content-box {{ margin: 20px auto; max-width: 700px; background: rgba(15,15,15,0.75); padding: 15px; border-radius: 10px; box-shadow: 0 4px 20px rgba(0,210,255,0.3); backdrop-filter: blur(15px); border: 1px solid rgba(0,210,255,0.2); position: relative; z-index: 1; }}
        h1 {{ color: #00d2ff; text-shadow: 0 0 15px rgba(0,210,255,0.6); position: relative; z-index: 1;}}
        .download-btn {{ background:linear-gradient(90deg, #00d2ff, #3a7bd5); padding:15px 30px; text-decoration:none; color:white; font-weight:bold; border-radius:5px; font-size:18px; box-shadow: 0 0 15px rgba(0,210,255,0.5); display: inline-block; position: relative; z-index: 1; transition: 0.3s; margin-bottom: 20px; }}
        .download-btn:hover {{ transform: scale(1.05); box-shadow: 0 0 25px rgba(0,210,255,0.8); }}
        .back-link {{ color:#ffde00; text-decoration:none; font-size:16px; font-weight:bold; position: relative; z-index: 1; display: inline-block; padding-bottom: 30px;}}
        .back-link:hover {{ text-shadow: 0 0 10px #ffde00; }}
    </style>
</head>
<body>
    <video autoplay muted loop playsinline id="bg-video">
        <source src="bg_network.mp4" type="video/mp4">
    </video>
    <h1>✅ {message}</h1>
    <div class="content-box">
        <h3 style="color: #ffde00; margin-top: 0;">🎬 Video Preview ({format_info})</h3>
        <video width="100%" controls autoplay style="border-radius: 8px; outline: none; background: #000; box-shadow: 0 4px 15px rgba(0,0,0,0.5);">
            <source src="{preview_link}" type="video/mp4">
        </video>
    </div>
    <br><a href='{download_link}' class='download-btn'>⬇️ Download MP4 Video</a>
    <br><a href='{back_link}' class='back-link'>⬅ Go Back</a>
</body>
</html>
"""

ERROR_HTML = """
<body style="background: #121212; color: white; font-family: Arial; text-align: center; padding-top: 100px;">
    <h1 style='color: #ff4d4d;'>❌ {message}</h1>
    <p>{details}</p><br><a href='{back_link}' style='color:#00d2ff; text-decoration:none;'>⬅ Go Back</a>
</body>
"""


def get_output_dimensions(aspect_ratio, resolution):
    is_720 = (resolution == '720p')
    if aspect_ratio == '9:16':
        return (720, 1280) if is_720 else (1080, 1920)
    if aspect_ratio == '1:1':
        return (720, 720) if is_720 else (1080, 1080)
    return (1280, 720) if is_720 else (1920, 1080)


def get_media_duration(media_path, fallback=15.0):
    try:
        cmd = [
            'ffprobe', '-i', media_path,
            '-show_entries', 'format=duration',
            '-v', 'quiet', '-of', 'csv=p=0'
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
        val = float(res.stdout.strip())
        return val if val > 0 else fallback
    except Exception:
        return fallback


def get_audio_duration(audio_path):
    return get_media_duration(audio_path, fallback=15.0)


def extract_youtube_id(url):
    pattern = r'(?:v=|\/shorts\/|youtu\.be\/|\/embed\/)([\w-]{11})'
    match = re.search(pattern, url)
    return match.group(1) if match else None


def format_srt_time(seconds):
    sec = max(0.0, float(seconds))
    hrs = int(sec // 3600)
    mins = int((sec % 3600) // 60)
    secs = int(sec % 60)
    millis = int(round((sec - int(sec)) * 1000))
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def hex_to_ass_color(hex_color):
    clean = (hex_color or "#ffffff").lstrip("#")
    if len(clean) != 6:
        return "&H00FFFFFF&"
    r, g, b = clean[0:2], clean[2:4], clean[4:6]
    return f"&H00{b}{g}{r}&".upper()


def build_srt_file(mode, lang, audio_path, offset_sec, custom_text, srt_path):
    entries = []
    shift = float(offset_sec or 0.0)

    if mode == "manual":
        for line in (custom_text or "").splitlines():
            parts = [p.strip() for p in line.split("|", 2)]
            if len(parts) == 3 and parts[2]:
                start_t = max(0.0, float(parts[0]) + shift)
                end_t = max(start_t + 0.5, float(parts[1]) + shift)
                entries.append((start_t, end_t, parts[2]))
    elif mode == "auto":
        from faster_whisper import WhisperModel
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_path, language=lang)
        for seg in segments:
            text = (seg.text or "").strip()
            if text:
                start_t = max(0.0, float(seg.start) + shift)
                end_t = max(start_t + 0.5, float(seg.end) + shift)
                entries.append((start_t, end_t, text))

    if not entries:
        return False

    with open(srt_path, "w", encoding="utf-8") as f:
        for idx, (st, et, txt) in enumerate(entries, start=1):
            f.write(f"{idx}\n{format_srt_time(st)} --> {format_srt_time(et)}\n{txt}\n\n")
    return True


def build_mixed_audio(
    voice_path, bgm_paths, sfx_path,
    voice_vol, bgm_vol, bgm_dur, bgm_start,
    fade_in, fade_out, sfx_start, sfx_vol,
    total_dur, out_audio
):
    cmd = ['ffmpeg', '-y', '-i', voice_path]
    filters = []
    mix_inputs = []

    v_chain = f"[0:a]volume={voice_vol},aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
    if fade_in > 0:
        v_chain += f",afade=t=in:st=0:d={fade_in}"
    if fade_out > 0:
        fo_start = max(0.0, total_dur - fade_out)
        v_chain += f",afade=t=out:st={fo_start}:d={fade_out}"
    v_chain += "[a_voice]"
    filters.append(v_chain)
    mix_inputs.append("[a_voice]")

    next_idx = 1
    valid_bgms = [bp for bp in (bgm_paths or []) if bp and os.path.exists(bp)]
    if valid_bgms:
        bgm_indices = []
        for bp in valid_bgms:
            if bgm_dur <= 0:
                cmd.extend(['-stream_loop', '-1', '-i', bp])
            else:
                cmd.extend(['-i', bp])
            bgm_indices.append(next_idx)
            next_idx += 1

        if len(bgm_indices) == 1:
            bgm_stream = f"[{bgm_indices[0]}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[b_norm];[b_norm]"
            filters.append(bgm_stream)
        else:
            norm_parts = []
            cat_inputs = []
            for k, b_idx in enumerate(bgm_indices):
                norm_parts.append(f"[{b_idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[b{k}]")
                cat_inputs.append(f"[b{k}]")
            filters.append(";".join(norm_parts))
            filters.append(f"{''.join(cat_inputs)}concat=n={len(bgm_indices)}:v=0:a=1[bgm_cat];[bgm_cat]")

        bgm_ops = []
        if bgm_dur > 0:
            bgm_ops.append(f"atrim=0:{bgm_dur},asetpts=PTS-STARTPTS")
        if bgm_start > 0:
            delay_bgm_ms = int(round(bgm_start * 1000))
            bgm_ops.append(f"adelay={delay_bgm_ms}:all=1")
        bgm_ops.append(f"volume={bgm_vol},aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a_bgm]")
        filters[-1] = filters[-1] + ",".join(bgm_ops)
        mix_inputs.append("[a_bgm]")

    if sfx_path and os.path.exists(sfx_path):
        cmd.extend(['-i', sfx_path])
        delay_ms = max(0, int(round(sfx_start * 1000)))
        filters.append(
            f"[{next_idx}:a]adelay={delay_ms}:all=1,volume={sfx_vol},aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a_sfx]"
        )
        mix_inputs.append("[a_sfx]")
        next_idx += 1

    if len(mix_inputs) == 1:
        cmd.extend([
            '-filter_complex', filters[0],
            '-map', '[a_voice]',
            '-c:a', 'aac', '-b:a', '192k',
            '-t', str(total_dur), out_audio
        ])
    else:
        mix_str = f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=2[a_out]"
        filters.append(mix_str)
        cmd.extend([
            '-filter_complex', ';'.join(filters),
            '-map', '[a_out]',
            '-c:a', 'aac', '-b:a', '192k',
            '-t', str(total_dur), out_audio
        ])

    subprocess.run(cmd, check=True)


@app.route('/')
def home():
    return send_file(os.path.join(FRONTEND_DIR, 'index.html'))


@app.route('/auto_editor')
def auto_editor():
    return send_file(os.path.join(FRONTEND_DIR, 'auto_editor.html'))


@app.route('/image_to_video')
def image_to_video():
    return send_file(os.path.join(FRONTEND_DIR, 'image_to_video.html'))


@app.route('/story_generator')
def story_generator():
    return send_file(os.path.join(FRONTEND_DIR, 'story_generator.html'))


@app.route('/trimmer')
def trimmer_page():
    return send_file(os.path.join(FRONTEND_DIR, 'trimmer.html'))


@app.route('/do_trim', methods=['POST'])
def do_trim():
    file = request.files.get('media_file')
    start = float(request.form.get('start', 0))
    end = float(request.form.get('end', 0))

    if not file or file.filename == '':
        return "No file selected!", 400

    safe_name = os.path.basename(file.filename)
    input_path = os.path.join(TEMP_FOLDER, f"in_{safe_name}")
    file.save(input_path)

    duration = end - start
    out_name = f"trimmed_{int(time.time())}_{safe_name}"
    out_path = os.path.join(TRIMMED_DIR, out_name)

    try:
        cmd = ['ffmpeg', '-y', '-ss', str(start), '-i', input_path, '-t', str(duration), out_path]
        subprocess.run(cmd, check=True)
        return send_file(out_path, as_attachment=True)
    except Exception as e:
        return f"Error cutting media: {str(e)}"


@app.route('/transcribe_link', methods=['POST'])
def transcribe_link():
    data = request.json or {}
    url = data.get('url', '')
    try:
        if 'youtube.com' in url or 'youtu.be' in url:
            video_id = extract_youtube_id(url)
            if not video_id:
                return jsonify({'status': 'error', 'message': 'Invalid YouTube URL'})

            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['en', 'hi', 'bn'])
            full_text = " ".join([item['text'] for item in transcript_list])
            summary = f"YouTube Video Transcript Extracted! Total words: {len(full_text.split())}."

            return jsonify({
                'status': 'success',
                'type': 'YouTube',
                'content': full_text,
                'summary': summary
            })
        else:
            return jsonify({'status': 'error', 'message': 'Please provide a valid YouTube link.'})
    except Exception:
        return jsonify({
            'status': 'error',
            'message': 'Could not fetch transcript for this video. (Subtitles might be disabled).'
        })


@app.route('/generate_story', methods=['POST'])
def generate_story():
    data = request.json or {}
    prompt = data.get('prompt', '')

    scenes = [
        {
            "scene_no": 1,
            "visual": "A cinematic wide shot of a traditional handloom weaver working in a wooden workshop during golden hour.",
            "voiceover": "जीवन में संघर्ष ही असली शिल्पकार है, जो हमें भीतर से मजबूत बनाता है।",
            "duration": 5.0
        },
        {
            "scene_no": 2,
            "visual": "Close-up shot of weathered hands skillfully weaving colorful threads on a vintage loom.",
            "voiceover": "हर एक धागा हमारी मेहनत और दृढ़ संकल्प की कहानी कहता है।",
            "duration": 6.0
        },
        {
            "scene_no": 3,
            "visual": "An inspiring cinematic portrait of the weaver looking out of the window with a peaceful smile.",
            "voiceover": "हार मत मानो, क्योंकि हर नई सुबह एक नई शुरुआत लेकर आती है।",
            "duration": 5.0
        }
    ]

    return jsonify({
        "status": "success",
        "story_title": f"Story based on: {prompt[:30]}...",
        "scenes": scenes
    })


@app.route('/upload', methods=['POST'])
def upload_files():
    audios = request.files.getlist('audio')
    media_files = request.files.getlist('media_files')
    bgm_files = request.files.getlist('bgm_files')
    sfx_file = request.files.get('sfx_file')

    if not audios or audios[0].filename == '':
        return ERROR_HTML.format(message="Audio file missing!", details="", back_link="/auto_editor")

    audio_paths = []
    for i, a in enumerate(audios):
        if a.filename:
            ap = os.path.join(UPLOAD_FOLDER, f'audio_{i}_{a.filename}')
            a.save(ap)
            audio_paths.append(ap)

    final_audio_path = os.path.join(UPLOAD_FOLDER, 'final_audio.aac')

    if len(audio_paths) > 1:
        concat_txt = os.path.join(UPLOAD_FOLDER, 'audio_concat.txt')
        with open(concat_txt, 'w', encoding='utf-8') as f:
            for ap in audio_paths:
                clean_ap = ap.replace('\\', '/')
                f.write(f"file '{clean_ap}'\n")
        subprocess.run(
            ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_txt, '-c:a', 'aac', final_audio_path],
            check=True
        )
    else:
        final_audio_path = audio_paths[0]

    bgm_paths = []
    for i, bf in enumerate(bgm_files):
        if bf and bf.filename:
            bp = os.path.join(TEMP_FOLDER, f"bgm_{i}_{os.path.basename(bf.filename)}")
            bf.save(bp)
            bgm_paths.append(bp)

    sfx_path = None
    if sfx_file and sfx_file.filename:
        sfx_path = os.path.join(TEMP_FOLDER, f"sfx_{os.path.basename(sfx_file.filename)}")
        sfx_file.save(sfx_path)

    media_items = []
    for mf in media_files:
        if mf.filename:
            path = os.path.join(UPLOAD_FOLDER, mf.filename)
            mf.save(path)
            type_str = 'video' if mf.mimetype.startswith('video') else 'image'
            media_items.append({'path': path, 'type': type_str, 'name': mf.filename})

    if not media_items:
        return ERROR_HTML.format(message="No media uploaded!", details="", back_link="/auto_editor")

    aspect_ratio = request.form.get('aspect_ratio', '16:9')
    resolution = request.form.get('resolution', '1080p')
    out_w, out_h = get_output_dimensions(aspect_ratio, resolution)
    hi_w, hi_h = out_w * 2, out_h * 2

    raw_audio_duration = get_audio_duration(final_audio_path)
    max_video_dur = float(request.form.get('max_video_duration', 0) or 0)
    target_duration = min(raw_audio_duration, max_video_dur) if max_video_dur > 0 else raw_audio_duration

    custom_durations = {}
    total_custom_time = 0.0
    for idx, item in enumerate(media_items):
        dur_val = request.form.get(f'duration_{idx}')
        if dur_val and dur_val.strip():
            try:
                val = float(dur_val)
                if val > 0:
                    custom_durations[idx] = val
                    total_custom_time += val
            except Exception:
                pass

    remaining_time = max(0.0, target_duration - total_custom_time)
    auto_count = len(media_items) - len(custom_durations)
    auto_duration = remaining_time / auto_count if auto_count > 0 else 0.0

    clip_paths = []
    clip_durations = []

    try:
        for idx, item in enumerate(media_items):
            out_clip = os.path.join(TEMP_FOLDER, f"clip_{idx}.mp4")
            item_duration = custom_durations.get(idx, auto_duration)
            if item_duration <= 0:
                item_duration = 1.0

            frames = max(1, int(round(item_duration * 30)))
            effect = request.form.get(f'effect_{idx}', 'auto')

            base_scale = f"scale={hi_w}:{hi_h}:force_original_aspect_ratio=decrease,pad={hi_w}:{hi_h}:(ow-iw)/2:(oh-ih)/2"
            norm_scale = f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2"
            fade_out_start = max(0.0, item_duration - 1.0)

            if item['type'] == 'image':
                z_step = f"{0.15 / frames:.6f}"
                zoom_in_vf = f"zoompan=z='min(zoom+{z_step},1.15)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={out_w}x{out_h}:fps=30"
                zoom_out_vf = f"zoompan=z='max(1.15-{z_step}*on,1.0)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={out_w}x{out_h}:fps=30"
                pan_right_vf = f"zoompan=z=1.15:d={frames}:x='(iw-iw/zoom)*(on/{frames})':y='ih/2-(ih/zoom/2)':s={out_w}x{out_h}:fps=30"
                pan_left_vf = f"zoompan=z=1.15:d={frames}:x='(iw-iw/zoom)*(1-on/{frames})':y='ih/2-(ih/zoom/2)':s={out_w}x{out_h}:fps=30"

                if effect == 'zoom_in':
                    vf = f"{base_scale},{zoom_in_vf},setsar=1"
                elif effect == 'zoom_out':
                    vf = f"{base_scale},{zoom_out_vf},setsar=1"
                elif effect == 'pan_left':
                    vf = f"{base_scale},{pan_left_vf},setsar=1"
                elif effect == 'pan_right':
                    vf = f"{base_scale},{pan_right_vf},setsar=1"
                elif effect == 'fade_in_out':
                    vf = f"{norm_scale},fade=t=in:st=0:d=1,fade=t=out:st={fade_out_start}:d=1,setsar=1"
                elif effect == 'blur':
                    vf = f"{base_scale},{zoom_in_vf},boxblur=10:2,setsar=1"
                elif effect == 'bw':
                    vf = f"{norm_scale},hue=s=0,setsar=1"
                else:
                    kb_effects = [zoom_in_vf, pan_right_vf, zoom_out_vf, pan_left_vf]
                    kb_vf = kb_effects[idx % 4]
                    vf = f"{base_scale},{kb_vf},setsar=1"

                cmd = [
                    'ffmpeg', '-y', '-loop', '1', '-i', item['path'],
                    '-t', str(item_duration), '-vf', vf,
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                    '-pix_fmt', 'yuv420p', '-r', '30', out_clip
                ]
            else:
                if effect == 'bw':
                    vf = f"{norm_scale},hue=s=0,setsar=1"
                elif effect == 'fade_in_out':
                    vf = f"{norm_scale},fade=t=in:st=0:d=1,fade=t=out:st={fade_out_start}:d=1,setsar=1"
                elif effect == 'blur':
                    vf = f"{norm_scale},boxblur=10:2,setsar=1"
                else:
                    vf = f"{norm_scale},setsar=1"

                cmd = [
                    'ffmpeg', '-y', '-stream_loop', '-1', '-i', item['path'],
                    '-t', str(item_duration), '-vf', vf,
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                    '-pix_fmt', 'yuv420p', '-r', '30', '-an', out_clip
                ]

            subprocess.run(cmd, check=True)
            clip_paths.append(out_clip)
            clip_durations.append(get_media_duration(out_clip, fallback=item_duration))

        merged_video_path = os.path.join(TEMP_FOLDER, 'merged_video.mp4')
        trans_type = request.form.get('transition_type', 'none')
        trans_dur = float(request.form.get('transition_duration', 0.6) or 0.6)

        if trans_type != 'none' and len(clip_paths) > 1:
            min_d = min(clip_durations)
            safe_t = min(trans_dur, max(0.1, min_d - 0.2))
            xf_cmd = ['ffmpeg', '-y']
            for cp in clip_paths:
                xf_cmd.extend(['-i', cp])

            xf_filters = []
            cur_dur = clip_durations[0]
            prev_label = "[0:v]"
            for i in range(1, len(clip_paths)):
                offset = max(0.0, cur_dur - safe_t)
                out_label = "[vout]" if i == len(clip_paths) - 1 else f"[v{i}]"
                xf_filters.append(
                    f"{prev_label}[{i}:v]xfade=transition={trans_type}:duration={safe_t}:offset={offset:.3f},format=yuv420p{out_label}"
                )
                cur_dur = cur_dur + clip_durations[i] - safe_t
                prev_label = out_label

            xf_cmd.extend([
                '-filter_complex', ';'.join(xf_filters),
                '-map', '[vout]',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-pix_fmt', 'yuv420p', '-r', '30',
                merged_video_path
            ])
            subprocess.run(xf_cmd, check=True)
        else:
            concat_file = os.path.join(UPLOAD_FOLDER, 'concat.txt')
            with open(concat_file, 'w', encoding='utf-8') as f:
                for cp in clip_paths:
                    clean_cp = cp.replace('\\', '/')
                    f.write(f"file '{clean_cp}'\n")
            subprocess.run([
                'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_file,
                '-c:v', 'copy', merged_video_path
            ], check=True)

        mixed_audio_path = os.path.join(TEMP_FOLDER, 'mixed_audio.aac')
        voice_vol = float(request.form.get('voice_vol', 1.0) or 1.0)
        bgm_vol = float(request.form.get('bgm_vol', 0.25) or 0.25)
        bgm_dur = float(request.form.get('bgm_duration', 0) or 0)
        bgm_start = float(request.form.get('bgm_start', 0) or 0)
        audio_fade_in = float(request.form.get('audio_fade_in', 0) or 0)
        audio_fade_out = float(request.form.get('audio_fade_out', 0) or 0)
        sfx_start = float(request.form.get('sfx_start', 0) or 0)
        sfx_vol = float(request.form.get('sfx_vol', 0.8) or 0.8)

        build_mixed_audio(
            final_audio_path, bgm_paths, sfx_path,
            voice_vol, bgm_vol, bgm_dur, bgm_start,
            audio_fade_in, audio_fade_out, sfx_start, sfx_vol,
            target_duration, mixed_audio_path
        )

        output_path = os.path.join(UPLOAD_FOLDER, 'final_video.mp4')
        combine_cmd = [
            'ffmpeg', '-y', '-i', merged_video_path,
            '-i', mixed_audio_path, '-c:v', 'copy', '-c:a', 'aac',
            '-b:a', '192k', '-t', str(target_duration), output_path
        ]
        subprocess.run(combine_cmd, check=True)

        sub_mode = request.form.get('subtitle_mode', 'off')
        if sub_mode in ('auto', 'manual'):
            sub_lang = request.form.get('subtitle_lang', 'bn')
            sub_font = request.form.get('subtitle_font', 'Arial')
            sub_size = request.form.get('subtitle_size', '28')
            sub_color = hex_to_ass_color(request.form.get('subtitle_color', '#ffffff'))
            sub_offset = request.form.get('subtitle_offset', '0')
            custom_subs = request.form.get('custom_subtitles', '')

            srt_file = os.path.join(TEMP_FOLDER, 'subtitles.srt')
            has_subs = build_srt_file(sub_mode, sub_lang, final_audio_path, sub_offset, custom_subs, srt_file)

            if has_subs:
                subbed_out = os.path.join(UPLOAD_FOLDER, 'final_video_subtitled.mp4')
                escaped_srt = srt_file.replace('\\', '/').replace(':', '\\:')
                style_str = f"FontName={sub_font},FontSize={sub_size},PrimaryColour={sub_color}"
                burn_cmd = [
                    'ffmpeg', '-y', '-i', output_path,
                    '-vf', f"subtitles='{escaped_srt}':force_style='{style_str}'",
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                    '-c:a', 'copy', subbed_out
                ]
                subprocess.run(burn_cmd, check=True)
                shutil.move(subbed_out, output_path)

        mins = int(target_duration // 60)
        secs = int(round(target_duration % 60))
        dur_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

        return SUCCESS_HTML.format(
            message="Final MP4 Video Generated Successfully!",
            format_info=f"MP4 • {aspect_ratio} • {resolution} • {dur_str}",
            preview_link="/preview_auto",
            download_link="/download",
            back_link="/auto_editor"
        )
    except Exception as e:
        return ERROR_HTML.format(message="Error generating video.", details=str(e), back_link="/auto_editor")


@app.route('/download')
def download():
    return send_file(os.path.join(UPLOAD_FOLDER, 'final_video.mp4'), as_attachment=True)


@app.route('/preview_auto')
def preview_auto():
    return send_file(os.path.join(UPLOAD_FOLDER, 'final_video.mp4'))


if __name__ == '__main__':
    print("🚀 AI Video Studio with Backend Storage is running on Port 5001!")
    app.run(host='0.0.0.0', debug=True, threaded=True, port=5001)