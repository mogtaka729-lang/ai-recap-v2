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

async def generate_audio(text, voice_model, output_path):
    communicate = edge_tts.Communicate(text, voice_model)
    await communicate.save(output_path)

def srt_to_text(srt_content):
    lines = srt_content.split('\n')
    text_lines = []
    for line in lines:
        line = line.strip()
        if not line or line.isdigit() or '-->' in line: continue
        text_lines.append(line)
    return ' '.join(text_lines)

@app.route('/')
def home():
    return "AI Recap Server V16 (Ultra Fast & Stable) is Active!"

# အဆင့် (၁) - ဗီဒီယိုမှ စာထုတ်ခြင်းနှင့် ဘာသာပြန်ခြင်း (Gemini Flash + FFMPEG)
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

        # မြန်နှုန်းမြင့် FFMPEG ဖြင့် အသံဆွဲထုတ်ခြင်း
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg_exe, '-i', filepath, '-vn', '-b:a', '64k', audio_filepath, '-y'], 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        with open(audio_filepath, "rb") as f:
            audio_data = base64.b64encode(f.read()).decode('utf-8')

        # Gemini 1.5 Flash ဖြင့် စာတန်းထိုးနှင့် ဘာသာပြန်ကို တစ်ပြိုင်နက်ထုတ်ယူခြင်း
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        prompt = f"Listen to this audio. Transcribe and translate the speech into natural {lang}. Output EXACTLY in standard SRT format (with accurate timestamps). Do not include any markdown formatting, triple backticks, or extra commentary."
        
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": "audio/mp3", "data": audio_data}}
                ]
            }],
            "generationConfig": {"temperature": 0.1}
        }
        
        res = requests.post(gemini_url, headers=headers, json=payload)
        if res.status_code != 200:
            err_msg = res.json().get('error', {}).get('message', 'Failed')
            return jsonify({"error": f"Gemini API Error: {err_msg}"}), 500
            
        srt_content = res.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        srt_content = re.sub(r'^```(srt)?\n?', '', srt_content, flags=re.MULTILINE)
        srt_content = re.sub(r'```$', '', srt_content, flags=re.MULTILINE).strip()

        if not srt_content or len(srt_content) < 10:
             return jsonify({"error": "No clear speech detected or Gemini failed to generate subtitles."}), 400

        with open(srt_filepath, 'w', encoding='utf-8') as f: f.write(srt_content)
        
        translated_text = srt_to_text(srt_content)
        with open(txt_filepath, 'w', encoding='utf-8') as f: f.write(translated_text)

        if os.path.exists(filepath): os.remove(filepath)
        if os.path.exists(audio_filepath): os.remove(audio_filepath)

        return jsonify({
            "file_id": unique_id, 
            "translated_text": translated_text, 
            "srt_url": f"/download/srt/{unique_id}",
            "txt_url": f"/download/txt/{unique_id}"
        })
    except Exception as e:
        return jsonify({"error": f"Server Error in Processing: {str(e)}"}), 500

# အဆင့် (၂) - အသံထုတ်ခြင်း (Microsoft Edge TTS)
@app.route('/step2_voice', methods=['POST'])
def step2_voice():
    try:
        data = request.json
        text = data.get('text')
        voice_choice = data.get('voice')
        lang = data.get('language')
        file_id = data.get('file_id')
        
        voice_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}_voice.mp3")
        voice_mapping = {
            "Myanmar": {"Nilar (Female)": "my-MM-NilarNeural", "Thiha (Male)": "my-MM-ThihaNeural"},
            "English": {"Nilar (Female)": "en-US-AriaNeural", "Thiha (Male)": "en-US-GuyNeural"},
            "Japanese": {"Nilar (Female)": "ja-JP-NanamiNeural", "Thiha (Male)": "ja-JP-KeitaNeural"},
            "Russian": {"Nilar (Female)": "ru-RU-SvetlanaNeural", "Thiha (Male)": "ru-RU-DmitryNeural"},
            "Thai": {"Nilar (Female)": "th-TH-PremwadeeNeural", "Thiha (Male)": "th-TH-NiwatNeural"}
        }
        selected_voice = voice_mapping.get(lang, {}).get(voice_choice, "my-MM-NilarNeural")
        
        asyncio.run(generate_audio(text, selected_voice, voice_filepath))
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
