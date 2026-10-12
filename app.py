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

async def generate_edge_audio(text, voice_model, output_path):
    communicate = edge_tts.Communicate(text, voice_model, rate="+0%")
    await communicate.save(output_path)

def clean_srt_and_text(raw_text):
    """SRT ထဲမှ အမှိုက်များနှင့် JSON ကုဒ်များကို ဖယ်ရှားပြီး သန့်စင်သော စာသားထုတ်ယူခြင်း"""
    cleaned = re.sub(r'^```(srt|json)?\n?', '', raw_text, flags=re.MULTILINE)
    cleaned = re.sub(r'```$', '', cleaned, flags=re.MULTILINE).strip()
    
    # JSON အပိုင်းအစများ ပါလာပါက ရှင်းထုတ်ခြင်း
    cleaned = re.sub(r'["\']?(text|start|end|end_delta)["\']?\s*:\s*', '', cleaned)
    cleaned = cleaned.replace('{', '').replace('}', '').replace('[', '').replace(']', '')

    lines = cleaned.split('\n')
    text_lines = []
    srt_lines = []
    
    for line in lines:
        l = line.strip().strip('",')
        if not l: continue
        if l.isdigit() or '-->' in l:
            srt_lines.append(l)
        else:
            srt_lines.append(l)
            text_lines.append(l)
            
    plain_text = " ".join(text_lines)
    srt_output = "\n".join(srt_lines)
    return srt_output, plain_text

@app.route('/')
def home():
    return "AI Recap Server (Nilar/Thiha Neural Voices) is Active!"

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

        # တိကျပြီး စကားလုံးမထပ်စေမည့် System Prompt
        prompt = (
            f"Listen to the speech in this audio clip carefully. "
            f"Transcribe and translate what is actually spoken into natural, coherent, and conversational {lang} (မြန်မာစကားပြော). "
            f"STRICT RULES:\n"
            f"1. Do NOT repeat phrases like 'ဟုတ်တယ်မလား' or loop sentences.\n"
            f"2. Ignore background music, silence, and sound effects.\n"
            f"3. Output strictly in standard SRT format with sequential numbers and accurate timestamps (00:00:00,000 --> 00:00:02,000).\n"
            f"4. Do NOT output JSON format, markdown backticks, or extra commentary."
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
            return jsonify({"error": "Gemini speech recognition failed."}), 500

        srt_content, clean_text = clean_srt_and_text(raw_result)

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
        return jsonify({"error": f"Server Error in Step 1: {str(e)}"}), 500

@app.route('/step2_voice', methods=['POST'])
def step2_voice():
    try:
        data = request.json
        text = data.get('text', '').strip()
        voice_choice = data.get('voice', 'Nilar (Female)')
        lang = data.get('language', 'Myanmar')
        file_id = data.get('file_id')
        
        if not text:
            return jsonify({"error": "No text provided for voice synthesis."}), 400

        voice_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}_voice.mp3")
        
        # နာမည်ကြီး Microsoft Neural Voices အစစ်အမှန်များ
        voice_mapping = {
            "Myanmar": {"Nilar (Female)": "my-MM-NilarNeural", "Thiha (Male)": "my-MM-ThihaNeural"},
            "English": {"en-US-AriaNeural": "en-US-AriaNeural"}
        }
        selected_voice = voice_mapping.get(lang, {}).get(voice_choice, "my-MM-NilarNeural")
        
        # Edge-TTS (Microsoft Neural Voice အစစ်) ဖြင့် အသံဖန်တီးခြင်း
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
