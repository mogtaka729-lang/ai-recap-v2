import os
import uuid
import requests
import asyncio
import edge_tts
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename
from moviepy.editor import VideoFileClip

app = Flask(__name__)
# CORS အားလုံးကို လက်ခံပေးရန် ဖွင့်ထားသည်
CORS(app, resources={r"/*": {"origins": "*"}})

UPLOAD_FOLDER = '/tmp/recap_files'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

async def generate_audio(text, voice_model, output_path):
    communicate = edge_tts.Communicate(text, voice_model)
    await communicate.save(output_path)

@app.route('/')
def home():
    return "AI Recap Server V9 (Oct 2026 - GPT-OSS Model) is Active!"

# အဆင့် (၁) - ဗီဒီယိုမှ SRT ထုတ်ခြင်း (Whisper အလုပ်လုပ်ဆဲဖြစ်သည်)
@app.route('/step1_srt', methods=['POST'])
def step1_srt():
    try:
        api_key = request.form.get('api_key')
        if not api_key: 
            return jsonify({"error": "API Key missing."}), 400
        if 'video' not in request.files: 
            return jsonify({"error": "No video uploaded."}), 400

        unique_id = str(uuid.uuid4())
        video_file = request.files['video']
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.mp4")
        audio_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.mp3")
        srt_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.srt")
        video_file.save(filepath)

        # အသံထုတ်ခြင်း
        try:
            video_clip = VideoFileClip(filepath)
            video_clip.audio.write_audiofile(audio_filepath, logger=None)
            video_clip.close()
        except Exception:
            return jsonify({"error": "Audio extraction failed."}), 500

        # Groq Whisper ဖြင့် စာသားထုတ်ခြင်း
        headers = {"Authorization": f"Bearer {api_key}"}
        with open(audio_filepath, 'rb') as f:
            files = {'file': ("audio.mp3", f, 'audio/mpeg')}
            data = {
                'model': 'whisper-large-v3-turbo', 
                'response_format': 'verbose_json',
                'temperature': '0.0',
                'prompt': 'This is a video transcription. Please transcribe clearly in the original language. Do not output traditional Chinese or simplified Chinese.'
            }
            res = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, files=files, data=data)
        
        if res.status_code != 200: 
            return jsonify({"error": f"Whisper Error: {res.json().get('error', {}).get('message', 'API Error')}"}), 500
        
        resp_data = res.json()
        original_text = resp_data.get('text', '').strip()
        
        if not original_text or "字幕" in original_text or "中文" in original_text:
            return jsonify({"error": "No clear speech detected in the video."}), 400

        srt_content = ""
        for i, seg in enumerate(resp_data.get('segments', [])):
            srt_content += f"{i+1}\n{format_time(seg['start'])} --> {format_time(seg['end'])}\n{seg['text'].strip()}\n\n"
        with open(srt_filepath, 'w', encoding='utf-8') as f: 
            f.write(srt_content)

        if os.path.exists(filepath): 
            os.remove(filepath)
        if os.path.exists(audio_filepath): 
            os.remove(audio_filepath)

        return jsonify({"file_id": unique_id, "original_text": original_text, "srt_url": f"/download/srt/{unique_id}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# အဆင့် (၂) - ဘာသာပြန်ခြင်း 
@app.route('/step2_translate', methods=['POST'])
def step2_translate():
    try:
        data = request.json
        api_key = data.get('api_key')
        text = data.get('text')
        lang = data.get('language')
        file_id = data.get('file_id')
        txt_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.txt")

        if lang == "English":
            translated_text = text
        else:
            chat_url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            prompt = f"Translate the following text to natural {lang}. Return ONLY the translated text:\n\n{text}"
            
            # ၂၀၂၆ အောက်တိုဘာလအထိ အလုပ်လုပ်သော gpt-oss-20b နှင့် qwen3.8-27b ကို ပြောင်းသုံးထားသည်
            models = ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
            translated_text = None
            last_error = ""

            for model in models:
                payload = {
                    "model": model, 
                    "messages": [
                        {"role": "system", "content": f"You are a professional translator converting text to {lang}."},
                        {"role": "user", "content": prompt}
                    ], 
                    "temperature": 0.3
                }
                res = requests.post(chat_url, headers=headers, json=payload)
                if res.status_code == 200:
                    translated_text = res.json()['choices'][0]['message']['content'].strip()
                    break  
                else:
                    last_error = res.json().get('error', {}).get('message', 'Failed')
            
            if not translated_text:
                return jsonify({"error": f"Groq Translate Error: {last_error}"}), 500

        with open(txt_filepath, 'w', encoding='utf-8') as f: 
            f.write(translated_text)
        return jsonify({"translated_text": translated_text, "txt_url": f"/download/txt/{file_id}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# အဆင့် (၃) - အသံထုတ်ခြင်း
@app.route('/step3_voice', methods=['POST'])
def step3_voice():
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
        return jsonify({"error": str(e)}), 500

@app.route('/download/srt/<file_id>')
def download_srt(file_id): 
    return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.srt"), as_attachment=True, download_name="subtitle.srt")

@app.route('/download/txt/<file_id>')
def download_txt(file_id): 
    return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.txt"), as_attachment=True, download_name="transcript.txt")

@app.route('/download/voice/<file_id>')
def download_voice(file_id): 
    return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}_voice.mp3"), as_attachment=True, download_name="voice_output.mp3", mimetype="audio/mpeg")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
