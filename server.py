from flask import Flask, request, jsonify
from backend import search_youtube_music, stream_music, get_music_thumbnail
import config

app = Flask(__name__)

@app.route('/search', methods=['GET'])
def search():
    query = request.args.get('query')
    if not query:
        return jsonify({"error": "Query parameter is required"}), 400
    results = search_youtube_music(query)
    return jsonify(results)

@app.route('/stream', methods=['GET'])
def stream():
    video_url = request.args.get('video_url')
    if not video_url:
        return jsonify({"error": "Video URL parameter is required"}), 400
    stream_url = stream_music(video_url)
    if stream_url:
        return jsonify({"stream_url": stream_url})
    return jsonify({"error": "Unable to stream music"}), 404

@app.route('/thumbnail', methods=['GET'])
def thumbnail():
    video_id = request.args.get('video_id')
    if not video_id:
        return jsonify({"error": "Video ID parameter is required"}), 400
    thumbnail_url = get_music_thumbnail(video_id)
    if thumbnail_url:
        return jsonify({"thumbnail_url": thumbnail_url})
    return jsonify({"error": "Thumbnail not found"}), 404

if __name__ == '__main__':
    app.run(host=config.SERVER_IP, port=config.SERVER_PORT)