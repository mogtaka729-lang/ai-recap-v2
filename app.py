import os
import uuid
import requests
import asyncio
import edge_tts
import subprocess
import imageio_ffmpeg
import base64
import re
import json
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

UPLOAD_FOLDER = '/tmp/recap_files'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

async def generate_edge_audio(text, voice_model, output_path):
    communicate = edge_tts.Communicate(text, voice_model)
    await communicate.save(output_path)

def google_tts_fallback(text, lang, output_path):
    """Microsoft 403 ပိတ်ပါက Google Voice ဖြင့် အလိုအလျောက် အစားထိုး အသံထုတ်ပေးမည့် စနစ်"""
    lang_code = 'my' if lang == 'Myanmar' else 'en'
    words = text.split()
    chunks = []
    curr = ""
    for w in words:
        if len(curr) + len(w) + 1 < 150:
            curr += (" " if curr else "") + w
        else:
            if curr: chunks.append(curr)
            curr = w
    if curr: chunks.append(curr)
    if not chunks: chunks = [text[:150]]

    audio_bytes = bytearray()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for chunk in chunks:
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&tl={lang_code}&client=tw-ob&q={requests.utils.quote(chunk)}"
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            audio_bytes.extend(r.content)
    
    if len(audio_bytes) > 0:
        with open(output_path, "wb") as f:
            f.write(audio_bytes)
        return True
    return False

def parse_subtitles_and_text(raw_output):
    """JSON သို့မဟုတ် SRT မှ စာသားများကို သန့်စင်ပြီးသား စာသားစစ်စစ်အဖြစ် ပြောင်းလဲခြင်း"""
    cleaned = re.sub(r'^```(json|srt)?\n?', '', raw_output, flags=re.MULTILINE)
    cleaned = re.sub(r'```$', '', cleaned, flags=re.MULTILINE).strip()
    
    # JSON ပုံစံဖြင့် ထွက်လာပါက
    try:
        match = re.search(r'\[\s*\{.*\}\s*\]', cleaned, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            srt_lines = []
            plain_texts = []
            for idx, item in enumerate(data, 1):
                start = item.get('start', '00:00:00,000')
                end = item.get('end', '00:00:02,000')
                text = item.get('text', '').strip()
                if text:
                    if start.count(':') == 1: start = f"00:{start}"
                    if end.count(':') == 1: end = f"00:{end}"
                    srt_lines.append(f"{idx}\n{start} --> {end}\n{text}\n")
                    plain_texts.append(text)
            return "\n".join(srt_lines), " ".join(plain_texts)
    except Exception:
        pass

    # Standard SRT ဖြစ်ပါက
    if '-->' in cleaned:
        lines = cleaned.split('\n')
        plain = []
        for line in lines:
            l = line.strip()
            if not l or l.isdigit() or '-->' in l: continue
            plain.append(l)
        return cleaned, " ".join(plain)
    
    return f"1\n00:00:00,000 --> 00:00:05,000\n{cleaned}\n", cleaned

@app.route('/')
def home():
    return "AI Recap Server (100% Complete & Stable) is Active!"

@app.route('/step1_process', methods=['POST'])
def step1_process():
    try:
        api_key = request.form.get('api_key')
        lang = request.form.get('language', 'Myanmar')
        
        if not api_key: return jsonify({"error": "Gemini API Key is required."}), 400
        if 'video' not in request.files: return jsonify({"error": "Video file is required."}), 400

        unique_id = str(uuid.uuid4())
        video_file = request.files['video']
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.mp4")
        audio_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.mp3")
        srt_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.srt")
        txt_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.txt")
        
        video_file.save(filepath)

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg_exe, '-i', filepath, '-vn', '-b:a', '64k', audio_filepath, '-y'], 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        with open(audio_filepath, "rb") as f:
            audio_data = base64.b64encode(f.read()).decode('utf-8')

        models_to_try = [
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash"
        ]

        prompt = f"Listen to this audio. Transcribe and translate the speech into natural {lang}. Output strictly in standard SRT format with timestamps. No explanations."
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": "audio/mp3", "data": audio_data}}
                ]
            }]
        }

        raw_result = None
        for model_name in models_to_try:
            gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            res = requests.post(gemini_url, headers={"Content-Type": "application/json"}, json=payload)
            if res.status_code == 200:
                try:
                    candidates = res.json().get('candidates', [])
                    if candidates and 'content' in candidates[0]:
                        raw_result = candidates[0]['content']['parts'][0]['text'].strip()
                        break
                except Exception:
                    continue

        if not raw_result:
            return jsonify({"error": "Gemini processing failed."}), 500

        srt_content, clean_text = parse_subtitles_and_text(raw_result)

        with open(srt_filepath, 'w', encoding='utf-8') as f: 
            f.write(srt_content)
        with open(txt_filepath, 'w', encoding='utf-8') as f: 
            f.write(clean_text)

        if os.path.exists(filepath): os.remove(filepath)
        if os.path.exists(audio_filepath): os.remove(audio_filepath)

        return jsonify({
            "file_id": unique_id, 
            "translated_text": clean_text, 
            "srt_url": f"/download/srt/{unique_id}",
            "txt_url": f"/download/txt/{unique_id}"
        })
    except Exception as e:
        return jsonify({"error": f"Server Error: {str(e)}"}), 500

@app.route('/step2_voice', methods=['POST'])
def step2_voice():
    try:
        data = request.json
        text = data.get('text', '')
        voice_choice = data.get('voice', 'Nilar (Female)')
        lang = data.get('language', 'Myanmar')
        file_id = data.get('file_id')
        
        voice_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}_voice.mp3")
        voice_mapping = {
            "Myanmar": {"Nilar (Female)": "my-MM-NilarNeural", "Thiha (Male)": "my-MM-ThihaNeural"},
            "English": {"en-US-AriaNeural": "en-US-AriaNeural"}
        }
        selected_voice = voice_mapping.get(lang, {}).get(voice_choice, "my-MM-NilarNeural")
        
        # ၁။ Edge-TTS ဖြင့် အရင်စမ်းသပ်မည်
        try:
            asyncio.run(generate_edge_audio(text, selected_voice, voice_filepath))
        except Exception:
            # ၂။ Microsoft 403 ဖြစ်ပါက Google Voice ဖြင့် အလိုအလျောက် ချက်ချင်း အစားထိုးထုတ်ယူမည်
            success = google_tts_fallback(text, lang, voice_filepath)
            if not success:
                return jsonify({"error": "Failed to synthesize audio."}), 500

        return jsonify({"voice_url": f"/download/voice/{file_id}"})
    except Exception as e:
        return jsonify({"error": f"Voice Error: {str(e)}"}), 500

@app.route('/download/srt/<file_id>')
def download_srt(file_id): return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.srt"), as_attachment=True, download_name="subtitle.srt")
@app.route('/download/txt/<file_id>')
def download_txt(file_id): return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.txt"), as_attachment=True, download_name="transcript.txt")
@app.route('/download/voice/<file_id>')
def download_voice(file_id): return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}_voice.mp3"), as_attachment=True, download_name="voice_output.mp3", mimetype="audio/mpeg")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
