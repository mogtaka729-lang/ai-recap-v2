import os
import uuid
import requests
import asyncio
import edge_tts
import subprocess
import imageio_ffmpeg
import base64
import re
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

UPLOAD_FOLDER = '/tmp/recap_files'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

async def generate_edge_audio_with_proxy(text, voice_model, output_path):
    """Render ၏ IP 403 ကို ကျော်ဖြတ်ရန် Proxy စနစ်ဖြင့် နီလာ/သီဟ အသံ ထုတ်ယူခြင်း"""
    # ၁။ တိုက်ရိုက်အရင် စမ်းသပ်ခြင်း
    try:
        communicate = edge_tts.Communicate(text, voice_model)
        await asyncio.wait_for(communicate.save(output_path), timeout=15)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return True
    except Exception:
        pass

    # ၂။ 403 တက်ပါက အခမဲ့ Elite HTTPS Proxy စာရင်း ရယူခြင်း
    proxies = []
    proxy_apis = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=4000&country=all&ssl=yes&anonymity=elite",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"
    ]
    
    for api_url in proxy_apis:
        try:
            r = requests.get(api_url, timeout=4)
            if r.status_code == 200:
                lines = [l.strip() for l in r.text.splitlines() if l.strip() and ":" in l]
                proxies.extend(lines[:12])
                if proxies:
                    break
        except Exception:
            continue

    # ၃။ Proxy များဖြင့် အလှည့်ကျ စမ်းသပ် ထုတ်ယူခြင်း
    for p in proxies:
        try:
            proxy_url = f"http://{p}"
            communicate = edge_tts.Communicate(text, voice_model, proxy=proxy_url)
            await asyncio.wait_for(communicate.save(output_path), timeout=12)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                return True
        except Exception:
            continue

    raise Exception("Proxy ချိတ်ဆက်မှု မအောင်မြင်ပါ။ စက္ကန့်အနည်းငယ်အကြာတွင် Try Again ပြန်နှိပ်ပေးပါ။")

@app.route('/')
def home():
    return "AI Movie Recap Server (Proxy Bypass Edition) is Active!"

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

        # အသံကို ကြည်လင်ပြတ်သားစေရန် 16kHz Mono သီးသန့်ဆွဲထုတ်ခြင်း (စာလုံးထပ်ခြင်းကို ကာကွယ်သည်)
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg_exe, '-i', filepath, '-vn', '-ac', '1', '-ar', '16000', '-b:a', '128k', audio_filepath, '-y'], 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        with open(audio_filepath, "rb") as f:
            audio_data = base64.b64encode(f.read()).decode('utf-8')

        models_to_try = [
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash"
        ]

        prompt = (
            f"You are a professional video translator and recap narrator. "
            f"Listen to the dialogue in this audio carefully. Translate and narrate the content in natural, fluent {lang} (မြန်မာစကားပြော).\n"
            f"STRICT RULES:\n"
            f"1. Ignore background music, sound effects, and silence.\n"
            f"2. Do NOT repeat senseless phrases like 'ဟုတ်တယ်မလား' or produce hallucinated loops.\n"
            f"3. Provide output strictly in standard SRT format with sequential numbering and timestamps (00:00:00,000 --> 00:00:02,000).\n"
            f"4. Do NOT output JSON format, markdown backticks, or explanatory commentary."
        )

        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": "audio/mp3", "data": audio_data}}
                ]
            }],
            "generationConfig": {"temperature": 0.2}
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
            return jsonify({"error": "Gemini speech transcription failed."}), 500

        # SRT နှင့် စာသားကို သန့်စင်စွာ ခွဲထုတ်ခြင်း
        srt_clean = re.sub(r'^```(srt)?\n?', '', raw_result, flags=re.MULTILINE)
        srt_clean = re.sub(r'```$', '', srt_clean, flags=re.MULTILINE).strip()

        lines = srt_clean.split('\n')
        text_lines = []
        for line in lines:
            l = line.strip()
            if not l or l.isdigit() or '-->' in l: continue
            text_lines.append(l)

        plain_text = " ".join(text_lines)
        if not plain_text: plain_text = srt_clean

        with open(srt_filepath, 'w', encoding='utf-8') as f: f.write(srt_clean)
        with open(txt_filepath, 'w', encoding='utf-8') as f: f.write(plain_text)

        if os.path.exists(filepath): os.remove(filepath)
        if os.path.exists(audio_filepath): os.remove(audio_filepath)

        return jsonify({
            "file_id": unique_id, 
            "translated_text": plain_text, 
            "srt_url": f"/download/srt/{unique_id}",
            "txt_url": f"/download/txt/{unique_id}"
        })
    except Exception as e:
        return jsonify({"error": f"Server Error in Step 1: {str(e)}"}), 500

@app.route('/step2_voice', methods=['POST'])
def step2_voice():
    try:
        data = request.json or {}
        text = data.get('text', '').strip()
        voice_choice = data.get('voice', 'Nilar (Female)')
        lang = data.get('language', 'Myanmar')
        file_id = data.get('file_id')

        if not text and file_id:
            txt_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.txt")
            if os.path.exists(txt_path):
                with open(txt_path, 'r', encoding='utf-8') as f: text = f.read().strip()

        if not text:
            return jsonify({"error": "No text available for voice synthesis."}), 400

        voice_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}_voice.mp3")
        
        voice_mapping = {
            "Myanmar": {"Nilar (Female)": "my-MM-NilarNeural", "Thiha (Male)": "my-MM-ThihaNeural"},
            "English": {"en-US-AriaNeural": "en-US-AriaNeural"}
        }
        selected_voice = voice_mapping.get(lang, {}).get(voice_choice, "my-MM-NilarNeural")

        # Proxy Fallback ဖြင့် အသံဖန်တီးခြင်း
        asyncio.run(generate_edge_audio_with_proxy(text, selected_voice, voice_filepath))

        return jsonify({"voice_url": f"/download/voice/{file_id}"})
    except Exception as e:
        return jsonify({"error": f"Voice Synthesis Error: {str(e)}"}), 500

@app.route('/download/srt/<file_id>')
def download_srt(file_id): return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.srt"), as_attachment=True, download_name="subtitle.srt")
@app.route('/download/txt/<file_id>')
def download_txt(file_id): return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.txt"), as_attachment=True, download_name="transcript.txt")
@app.route('/download/voice/<file_id>')
def download_voice(file_id): return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}_voice.mp3"), as_attachment=True, download_name="voice_output.mp3", mimetype="audio/mpeg")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
