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
    communicate = edge_tts.Communicate(text, voice_model, rate="+0%")
    await communicate.save(output_path)

@app.route('/')
def home():
    return "AI Movie Recap Server (Final Stable Nilar/Thiha) is Active!"

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

        # FFMPEG ဖြင့် အသံဆွဲထုတ်ခြင်း
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg_exe, '-i', filepath, '-vn', '-b:a', '64k', audio_filepath, '-y'], 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        with open(audio_filepath, "rb") as f:
            audio_data = base64.b64encode(f.read()).decode('utf-8')

        models_to_try = [
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash"
        ]

        # Movie Recap အတွက် ဇာတ်လမ်းပြောစာသားနှင့် SRT ကို တိကျစွာ ခွဲထုတ်ခိုင်းခြင်း
        prompt = (
            f"You are a professional Movie Recap creator. Listen to the speech and sound in this audio. "
            f"Create a natural, engaging {lang} (မြန်မာစကားပြော) movie recap narration and timestamped SRT subtitles.\n"
            f"Output MUST be a single valid JSON object with EXACTLY two fields:\n"
            f"1. 'script': Clean, storytelling Myanmar movie recap narration text for voiceover. Do NOT repeat senseless phrases like 'ဟုတ်တယ်မလား'. Make it sound like a captivating story narrator.\n"
            f"2. 'srt': Standard SRT subtitles with sequence numbers and timestamps (00:00:00,000 --> 00:00:03,000).\n"
            f"Output ONLY raw JSON format without markdown backticks."
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
            return jsonify({"error": "Gemini analysis failed."}), 500

        # JSON Parse ပြုလုပ်ခြင်း
        cleaned_json = re.sub(r'^```(json)?\s*', '', raw_result, flags=re.MULTILINE)
        cleaned_json = re.sub(r'\s*```$', '', cleaned_json, flags=re.MULTILINE).strip()

        script_text = ""
        srt_content = ""

        try:
            match = re.search(r'\{.*\}', cleaned_json, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                script_text = data.get('script', '').strip()
                srt_content = data.get('srt', '').strip()
        except Exception:
            pass

        # အကယ်၍ JSON parse မဖြစ်ပါက လုံခြုံရေးအရ စာသားကို တိုက်ရိုက်ဆွဲထုတ်ခြင်း
        if not script_text:
            plain_lines = [l.strip() for l in cleaned_json.split('\n') if l.strip() and not l.strip().isdigit() and '-->' not in l]
            script_text = " ".join(plain_lines)
        if not srt_content:
            srt_content = f"1\n00:00:00,000 --> 00:00:05,000\n{script_text}\n"

        if not script_text:
            script_text = "ဇာတ်လမ်းပြော စာသားကို ရယူနေပါသည်။"

        with open(srt_filepath, 'w', encoding='utf-8') as f: 
            f.write(srt_content)
        with open(txt_filepath, 'w', encoding='utf-8') as f: 
            f.write(script_text)

        if os.path.exists(filepath): os.remove(filepath)
        if os.path.exists(audio_filepath): os.remove(audio_filepath)

        return jsonify({
            "file_id": unique_id, 
            "translated_text": script_text, 
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
        
        # စာသားပျောက်ဆုံးမှု မရှိစေရန် ဆာဗာဖိုင်ထဲမှပါ အလိုအလျောက် ပြန်ဖတ်သည့် စနစ်
        if not text and file_id:
            txt_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.txt")
            if os.path.exists(txt_path):
                with open(txt_path, 'r', encoding='utf-8') as f:
                    text = f.read().strip()

        if not text:
            text = "ဇာတ်လမ်းကို စတင်တင်ဆက်ပေးပါမည်။"

        voice_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}_voice.mp3")
        
        # နာမည်ကြီး Microsoft Edge-TTS Nilar / Thiha Voices
        voice_mapping = {
            "Myanmar": {"Nilar (Female)": "my-MM-NilarNeural", "Thiha (Male)": "my-MM-ThihaNeural"},
            "English": {"en-US-AriaNeural": "en-US-AriaNeural"}
        }
        selected_voice = voice_mapping.get(lang, {}).get(voice_choice, "my-MM-NilarNeural")
        
        asyncio.run(generate_edge_audio(text, selected_voice, voice_filepath))

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
    app.run(host='0.0.0.0', port=5000)
