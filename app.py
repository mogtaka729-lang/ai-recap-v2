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
# CORS ကို အားလုံးအတွက် ဖွင့်ထားသည်
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
    return "AI Recap Server V12 (Gemini API for Translation) is Active!"

# အဆင့် (၁) - ဗီဒီယိုမှ SRT ထုတ်ခြင်း (Groq Whisper အသုံးပြုဆဲ)
@app.route('/step1_srt', methods=['POST'])
def step1_srt():
    try:
        # ဒီအဆင့်အတွက် Groq API Key လိုအပ်ပါသည်
        api_key = request.form.get('api_key')
        if not api_key: return jsonify({"error": "Groq API Key is required for Step 1."}), 400
        if 'video' not in request.files: return jsonify({"error": "Video file is required."}), 400

        unique_id = str(uuid.uuid4())
        video_file = request.files['video']
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.mp4")
        audio_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.mp3")
        srt_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.srt")
        video_file.save(filepath)

        try:
            video_clip = VideoFileClip(filepath)
            video_clip.audio.write_audiofile(audio_filepath, logger=None)
            video_clip.close()
        except Exception as e:
            return jsonify({"error": f"Failed to extract audio from video: {str(e)}"}), 500

        # Groq Whisper (whisper-large-v3-turbo)
        headers = {"Authorization": f"Bearer {api_key}"}
        with open(audio_filepath, 'rb') as f:
            files = {'file': ("audio.mp3", f, 'audio/mpeg')}
            data = {
                'model': 'whisper-large-v3-turbo', 
                'response_format': 'verbose_json',
                'temperature': '0.0',
                'prompt': 'This is a transcription. Please output in the original language. Do not output Chinese.'
            }
            res = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, files=files, data=data)
        
        if res.status_code != 200: 
            err_msg = res.json().get('error', {}).get('message', 'Unknown Groq Error')
            return jsonify({"error": f"Whisper API Error: {err_msg}"}), 500
        
        resp_data = res.json()
        original_text = resp_data.get('text', '').strip()
        
        if not original_text:
            return jsonify({"error": "No clear speech detected in the video."}), 400

        srt_content = ""
        for i, seg in enumerate(resp_data.get('segments', [])):
            srt_content += f"{i+1}\n{format_time(seg['start'])} --> {format_time(seg['end'])}\n{seg['text'].strip()}\n\n"
        with open(srt_filepath, 'w', encoding='utf-8') as f: f.write(srt_content)

        if os.path.exists(filepath): os.remove(filepath)
        if os.path.exists(audio_filepath): os.remove(audio_filepath)

        return jsonify({"file_id": unique_id, "original_text": original_text, "srt_url": f"/download/srt/{unique_id}"})
    except Exception as e:
        return jsonify({"error": f"Server Error in Step 1: {str(e)}"}), 500

# အဆင့် (၂) - ဘာသာပြန်ခြင်း (Google Gemini API အား ပြောင်းလဲ အသုံးပြုထားသည်)
@app.route('/step2_translate', methods=['POST'])
def step2_translate():
    try:
        data = request.json
        if not data: return jsonify({"error": "Invalid JSON data received."}), 400
        
        # UI မှ လာသော API Key အား Gemini API Key အဖြစ် အသုံးပြုမည် 
        gemini_api_key = data.get('api_key')
        text = data.get('text')
        lang = data.get('language')
        file_id = data.get('file_id')
        
        if not all([gemini_api_key, text, lang, file_id]):
            return jsonify({"error": "Missing required parameters (api_key, text, language, or file_id)."}), 400

        txt_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.txt")

        if lang == "English":
            translated_text = text
        else:
            # Google Gemini API ချိတ်ဆက်ခြင်း
            gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_api_key}"
            headers = {"Content-Type": "application/json"}
            prompt = f"Translate the following text to natural {lang}. Return ONLY the translated text without any other comments:\n\n{text}"
            
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3}
            }
            
            res = requests.post(gemini_url, headers=headers, json=payload)
            
            if res.status_code == 200:
                # Gemini မှ ပြန်လာသော စာသားကို ဖမ်းယူခြင်း
                try:
                    translated_text = res.json()['candidates'][0]['content']['parts'][0]['text'].strip()
                except (KeyError, IndexError):
                    return jsonify({"error": "Failed to parse Gemini response."}), 500
            else:
                err_msg = res.json().get('error', {}).get('message', 'Unknown Gemini Error')
                return jsonify({"error": f"Gemini Translate Error: {err_msg}"}), 500

        with open(txt_filepath, 'w', encoding='utf-8') as f: f.write(translated_text)
        return jsonify({"translated_text": translated_text, "txt_url": f"/download/txt/{file_id}"})
    except Exception as e:
        return jsonify({"error": f"Server Error in Step 2: {str(e)}"}), 500

# အဆင့် (၃) - အသံထုတ်ခြင်း (Microsoft Edge TTS)
@app.route('/step3_voice', methods=['POST'])
def step3_voice():
    try:
        data = request.json
        if not data: return jsonify({"error": "Invalid JSON data received."}), 400
        
        text = data.get('text')
        voice_choice = data.get('voice')
        lang = data.get('language')
        file_id = data.get('file_id')
        
        if not all([text, voice_choice, lang, file_id]):
            return jsonify({"error": "Missing required parameters for voice generation."}), 400

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
        return jsonify({"error": f"Server Error in Step 3: {str(e)}"}), 500

@app.route('/download/srt/<file_id>')
def download_srt(file_id): return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.srt"), as_attachment=True, download_name="subtitle.srt")
@app.route('/download/txt/<file_id>')
def download_txt(file_id): return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.txt"), as_attachment=True, download_name="transcript.txt")
@app.route('/download/voice/<file_id>')
def download_voice(file_id): return send_file(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}_voice.mp3"), as_attachment=True, download_name="voice_output.mp3", mimetype="audio/mpeg")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
