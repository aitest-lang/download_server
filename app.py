from flask import Flask, render_template, request, send_file, jsonify, abort
from flask_socketio import SocketIO, emit
import os
import libtorrent as lt
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor
import shutil

app = Flask(__name__)
socketio = SocketIO(app)  # Initialize SocketIO

# Directory to store downloaded files
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Dictionary to track torrent download progress
torrent_progress = {}

# Time after which files should be deleted (in seconds)
FILE_LIFETIME = 24 * 60 * 60  # 24 hours

# Thread pool for background tasks
executor = ThreadPoolExecutor(max_workers=5)

# Set debug mode based on environment variable
DEBUG = os.getenv("FLASK_ENV") == "development"

# Threshold for disk usage (in percentage)
DISK_USAGE_THRESHOLD = 90


@app.route("/", methods=["GET"])
def home():
    return render_template("home.html")


@app.route("/direct-download", methods=["GET", "POST"])
def direct_download():
    if request.method == "POST":
        file_url = request.form.get("file_url")
        if not file_url:
            return "Please provide a valid file URL.", 400

        # Submit the download task to the thread pool
        future = executor.submit(download_file_in_background, file_url)
        file_name = os.path.basename(file_url.split("?")[0])
        file_path = f"/play/{file_name}" if file_name.lower().endswith((".mp4", ".webm", ".ogg", ".mp3", ".wav")) else f"/download/{file_name}"
        return render_template("processing.html", file_path=file_path)

    return render_template("direct_download.html")


def download_file_in_background(file_url):
    try:
        # Download the file from the provided URL
        response = requests.get(file_url, stream=True)
        response.raise_for_status()

        # Extract the file name from the URL
        file_name = os.path.basename(file_url.split("?")[0])
        file_path = os.path.join(DOWNLOAD_DIR, file_name)

        total_size = int(response.headers.get("content-length", 0))
        downloaded_size = 0

        # Save the file locally
        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)
                    progress = (downloaded_size / total_size) * 100 if total_size > 0 else 0
                    socketio.emit("download_progress", {"progress": progress})  # Emit progress via WebSocket
        print(f"File downloaded successfully: {file_path}")
    except Exception as e:
        print(f"Error downloading file: {str(e)}")


@app.route("/torrent-download", methods=["GET", "POST"])
def torrent_download():
    if request.method == "POST":
        file_url = request.form.get("file_url")
        if not file_url:
            return "Please provide a valid torrent URL or magnet link.", 400

        # Check if it's a torrent file or magnet link
        if file_url.endswith(".torrent") or file_url.startswith("magnet:?"):
            # Generate the info_hash for the torrent/magnet link
            if file_url.startswith("magnet:?"):
                # Extract info_hash from magnet link
                import re
                match = re.search(r"xt=urn:btih:([a-zA-Z0-9]+)", file_url)
                if not match:
                    return "Invalid magnet link.", 400
                info_hash = match.group(1)
            else:
                # Download the .torrent file to extract info_hash
                response = requests.get(file_url)
                torrent_file = os.path.join(DOWNLOAD_DIR, "temp.torrent")
                with open(torrent_file, "wb") as f:
                    f.write(response.content)
                info = lt.torrent_info(torrent_file)
                info_hash = str(info.info_hash())

            # Start torrent download in a separate thread
            executor.submit(download_torrent, file_url, info_hash)

            # Pass the info_hash to the progress page
            return render_template("progress.html", info_hash=info_hash)
        else:
            return "Unsupported file type. Please provide a torrent file or magnet link.", 400

    return render_template("torrent_download.html")


@socketio.on("get_torrent_progress")
def handle_torrent_progress(info_hash):
    # Emit the current progress for the requested torrent
    progress = torrent_progress.get(info_hash, {"progress": 0, "status": "Downloading..."})
    emit("torrent_progress", progress)


def download_torrent(file_url, info_hash):
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
        socketio.emit("torrent_progress", {"info_hash": info_hash, "progress": progress})  # Emit progress via WebSocket
        time.sleep(1)

    # Mark as completed
    torrent_progress[info_hash]["status"] = "Download Complete!"
    print(f"Torrent download complete: {info_hash}")


@app.route("/media-player", methods=["GET", "POST"])
def media_player():
    if request.method == "POST":
        file_url = request.form.get("file_url")
        if not file_url:
            return "Please provide a valid file URL.", 400

        # Submit the download task to the thread pool
        future = executor.submit(download_file_in_background, file_url)
        file_name = os.path.basename(file_url.split("?")[0])
        file_path = f"/play/{file_name}" if file_name.lower().endswith((".mp4", ".webm", ".ogg", ".mp3", ".wav")) else f"/download/{file_name}"

    # List all playable media files in the downloads directory
    media_files = []
    for filename in os.listdir(DOWNLOAD_DIR):
        if filename.lower().endswith((".mp4", ".webm", ".ogg", ".mp3", ".wav")):
            media_files.append(filename)
    return render_template("media_player.html", media_files=media_files)


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


def cleanup_old_files():
    """
    Deletes files in the DOWNLOAD_DIR that are older than FILE_LIFETIME.
    Also deletes files if the disk is full.
    """
    current_time = time.time()

    # Get all files sorted by modification time (oldest first)
    files = [
        os.path.join(DOWNLOAD_DIR, f)
        for f in os.listdir(DOWNLOAD_DIR)
        if os.path.isfile(os.path.join(DOWNLOAD_DIR, f))
    ]
    files.sort(key=lambda x: os.path.getmtime(x))

    for file_path in files:
        # Delete files older than FILE_LIFETIME
        file_age = current_time - os.path.getmtime(file_path)
        if file_age > FILE_LIFETIME:
            print(f"Deleting old file: {file_path}")
            os.remove(file_path)

    # Check disk usage again after deleting old files
    cleanup_if_disk_full()


def cleanup_if_disk_full():
    """
    Checks the disk usage and deletes old files if the disk is full.
    """
    total, used, free = shutil.disk_usage("/")
    disk_usage_percent = (used / total) * 100

    print(f"Disk usage: {disk_usage_percent:.2f}%")

    if disk_usage_percent > DISK_USAGE_THRESHOLD:
        print("Disk usage exceeded threshold. Deleting old files...")
        cleanup_old_files()


def start_cleanup_task():
    """
    Starts a periodic task to clean up old files and check disk usage.
    """
    cleanup_old_files()  # Clean up old files immediately on startup
    cleanup_if_disk_full()  # Check disk usage and clean up if necessary

    # Schedule the next cleanup after FILE_LIFETIME seconds
    threading.Timer(FILE_LIFETIME, start_cleanup_task).start()


# Start the cleanup task when the Flask app starts
start_cleanup_task()


if __name__ == "__main__":
    # Run the Flask development server only if executed directly
    socketio.run(app, debug=DEBUG)
