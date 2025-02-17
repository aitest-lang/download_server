from flask import Flask, render_template, request, send_file, jsonify, abort
import os
import libtorrent as lt
import time
import threading
import requests

app = Flask(__name__)

# Directory to store downloaded files
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Dictionary to track torrent download progress
torrent_progress = {}

# Time after which files should be deleted (in seconds)
FILE_LIFETIME = 24 * 60 * 60  # 24 hours

# Set debug mode based on environment variable
DEBUG = os.getenv("FLASK_ENV") == "development"

@app.route("/", methods=["GET"])
def home():
    return render_template("home.html")

@app.route("/direct-download", methods=["GET", "POST"])
def direct_download():
    if request.method == "POST":
        file_url = request.form.get("file_url")
        if not file_url:
            return "Please provide a valid file URL.", 400

        try:
            # Download the file from the provided URL
            response = requests.get(file_url, stream=True)
            response.raise_for_status()

            # Extract the file name from the URL
            file_name = os.path.basename(file_url.split("?")[0])
            file_path = os.path.join(DOWNLOAD_DIR, file_name)

            # Save the file locally
            with open(file_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Determine the MIME type based on the file extension
            if file_name.lower().endswith((".mp4", ".webm", ".ogg")):
                return render_template("play.html", file_path=f"/play/{file_name}")
            elif file_name.lower().endswith((".mp3", ".wav", ".ogg")):
                return render_template("play.html", file_path=f"/play/{file_name}")
            else:
                return render_template("download.html", file_path=f"/download/{file_name}")

        except Exception as e:
            return f"Error processing the file: {str(e)}", 500

    return render_template("direct_download.html")


@app.route("/torrent-download", methods=["GET", "POST"])
def torrent_download():
    if request.method == "POST":
        file_url = request.form.get("file_url")
        if not file_url:
            return "Please provide a valid torrent URL or magnet link.", 400

        # Check if it's a torrent file or magnet link
        if file_url.endswith(".torrent") or file_url.startswith("magnet:?"):
            # Start torrent download in a separate thread
            thread = threading.Thread(target=download_torrent, args=(file_url,))
            thread.start()
            return render_template("progress.html", file_url=file_url)
        else:
            return "Unsupported file type. Please provide a torrent file or magnet link.", 400

    return render_template("torrent_download.html")


@app.route("/progress/<info_hash>")
def get_progress(info_hash):
    # Return the progress of the torrent download
    progress = torrent_progress.get(info_hash, {"progress": 0, "status": "Downloading..."})
    return jsonify(progress)


@app.route("/play/<filename>")
def play_file(filename):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(file_path):
        abort(404)

    # Serve the file for playback
    return send_file(file_path)


@app.route("/download/<filename>")
def download_file(filename):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(file_path):
        abort(404)

    # Serve the file for download
    return send_file(file_path, as_attachment=True)


def download_torrent(file_url):
    ses = lt.session()
    ses.listen_on(6881, 6891)

    # Add the torrent to the session
    if file_url.startswith("magnet:?"):
        handle = lt.add_magnet_uri(ses, file_url, {"save_path": DOWNLOAD_DIR})
    else:
        # Download the .torrent file first
        response = requests.get(file_url)
        torrent_file = os.path.join(DOWNLOAD_DIR, "temp.torrent")
        with open(torrent_file, "wb") as f:
            f.write(response.content)
        info = lt.torrent_info(torrent_file)
        handle = ses.add_torrent({"ti": info, "save_path": DOWNLOAD_DIR})

    info_hash = str(handle.info_hash())
    torrent_progress[info_hash] = {"progress": 0, "status": "Downloading..."}

    # Wait for metadata (for magnet links)
    while not handle.has_metadata():
        time.sleep(1)

    # Monitor download progress
    while not handle.is_seed():
        s = handle.status()
        progress = s.progress * 100
        torrent_progress[info_hash] = {
            "progress": progress,
            "status": f"Downloading... {progress:.2f}%",
        }
        time.sleep(1)

    # Mark as completed
    torrent_progress[info_hash]["status"] = "Download Complete!"
    print(f"Torrent download complete: {info_hash}")


def cleanup_old_files():
    """
    Deletes files in the DOWNLOAD_DIR that are older than FILE_LIFETIME.
    """
    current_time = time.time()
    for filename in os.listdir(DOWNLOAD_DIR):
        file_path = os.path.join(DOWNLOAD_DIR, filename)
        if os.path.isfile(file_path):
            file_age = current_time - os.path.getmtime(file_path)  # Age of the file in seconds
            if file_age > FILE_LIFETIME:
                print(f"Deleting old file: {file_path}")
                os.remove(file_path)


def start_cleanup_task():
    """
    Starts a periodic task to clean up old files.
    """
    cleanup_old_files()  # Run cleanup immediately on startup
    # Schedule the next cleanup after FILE_LIFETIME seconds
    threading.Timer(FILE_LIFETIME, start_cleanup_task).start()


# Start the cleanup task when the Flask app starts
start_cleanup_task()


if __name__ == "__main__":
    # Run the Flask development server only if executed directly
    app.run(debug=DEBUG)