import os
import uuid
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename
from moviepy.editor import VideoFileClip
from gtts import gTTS

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = '/tmp/recap_files'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# အချိန်ကိုက် SRT အတွက် အချိန်ပြောင်းပေးသည့်စနစ်
def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

@app.route('/')
def home():
    return "AI Recap Server V2 is Active!"

@app.route('/process', methods=['POST'])
def process_video():
    unique_id = str(uuid.uuid4())
    filepath = None
    audio_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.mp3")
    srt_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.srt")
    txt_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}.txt")
    voice_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}_voice.mp3")

    try:
        api_key = request.form.get('api_key')
        language = request.form.get('language', 'English')
        
        if not api_key:
            return jsonify({"error": "Groq API Key is missing. Please login."}), 400

        if 'video' not in request.files:
            return jsonify({"error": "No video uploaded."}), 400
            
        video_file = request.files['video']
        filename = secure_filename(video_file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}_{filename}")
        video_file.save(filepath)

        try:
            video_clip = VideoFileClip(filepath)
            video_clip.audio.write_audiofile(audio_filepath, logger=None)
            video_clip.close()
        except Exception as e:
            return jsonify({"error": f"Audio extraction failed: No sound detected or video issue."}), 500

        # ၁။ Groq ဖြင့် စာသားထုတ်ခြင်း (JSON ပုံစံဖြင့် တောင်းခံ၍ Error ရှောင်ခြင်း)
        headers_audio = {"Authorization": f"Bearer {api_key}"}
        with open(audio_filepath, 'rb') as f:
            files = {'file': ("audio.mp3", f, 'audio/mpeg')}
            data = {'model': 'whisper-large-v3', 'response_format': 'verbose_json'}
            res_srt = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers_audio, files=files, data=data)
        
        # Groq က ငြင်းခဲ့လျှင် အကြောင်းရင်းအမှန်ကို ဖော်ပြပေးရန်
        if res_srt.status_code != 200:
            err_msg = res_srt.json().get('error', {}).get('message', 'Unknown Groq Error')
            return jsonify({"error": f"Groq AI Error: {err_msg}"}), 500
            
        resp_data = res_srt.json()
        transcript_text = resp_data.get('text', '')
        
        # ၂။ သေသပ်သော SRT အဖြစ် ပြောင်းလဲခြင်း
        srt_content = ""
        segments = resp_data.get('segments', [])
        for i, seg in enumerate(segments):
            start = format_time(seg['start'])
            end = format_time(seg['end'])
            text = seg['text'].strip()
            srt_content += f"{i+1}\n{start} --> {end}\n{text}\n\n"

        with open(srt_filepath, 'w', encoding='utf-8') as f:
            f.write(srt_content)

        final_transcript = transcript_text

        # ၃။ ဘာသာပြန်ခြင်း
        if language != "English":
            chat_url = "https://api.groq.com/openai/v1/chat/completions"
            headers_chat = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            prompt = f"Translate the following text to natural {language}. Return ONLY the translated text without timestamps:\n\n{transcript_text}"
            chat_payload = {
                "model": "mixtral-8x7b-32768",
                "messages": [
                    {"role": "system", "content": f"You are a professional translator translating to {language}."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3
            }
            chat_res = requests.post(chat_url, headers=headers_chat, json=chat_payload)
            
            if chat_res.status_code == 200:
                final_transcript = chat_res.json()['choices'][0]['message']['content']
            else:
                err_msg = chat_res.json().get('error', {}).get('message', 'Translation Failed')
                return jsonify({"error": f"Groq Translate Error: {err_msg}"}), 500

        with open(txt_filepath, 'w', encoding='utf-8') as f:
            f.write(final_transcript)

        # ၄။ Text-to-Speech အသံပြောင်းခြင်း
        lang_code = 'my' if language == 'Myanmar' else 'en'
        tts = gTTS(text=final_transcript, lang=lang_code)
        tts.save(voice_filepath)

        return jsonify({
            "message": "Success",
            "transcript": final_transcript,
            "srt_url": f"/download/srt/{unique_id}",
            "txt_url": f"/download/txt/{unique_id}",
            "voice_url": f"/download/voice/{unique_id}"
        })

    except Exception as e:
        return jsonify({"error": f"Server Error: {str(e)}"}), 500
    finally:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
        if audio_filepath and os.path.exists(audio_filepath):
            os.remove(audio_filepath)

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
