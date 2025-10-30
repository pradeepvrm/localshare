import os
import json
import time
import uuid
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
import threading

# --- Configuration ---
SERVER_PORT = 5000     # Port for Flask HTTP server
UPLOAD_FOLDER = 'uploads' # Folder to store received files
USER_TIMEOUT = 30      # Seconds to consider a user "offline" after last heartbeat

# --- Global State ---
# Thread-safe storage for logged-in users
# Format: { "user_id": {"name": "Alice", "last_seen": timestamp} }
users = {}
users_lock = threading.Lock()

# Thread-safe storage for messages
# Format: { "msg_id": "...", "from_name": "...", "to_user_id": "...", "type": "...", "content": "...", "timestamp": ... }
messages = []
messages_lock = threading.Lock()

# Use a basic thread for pruning users
# import threading

def prune_users():
    """Removes users that haven't sent a heartbeat in a while."""
    while True:
        time.sleep(USER_TIMEOUT)
        now = time.time()
        with users_lock:
            # We must create a new dict, as we can't modify it while iterating
            active_users = {}
            for user_id, info in users.items():
                if now - info['last_seen'] < USER_TIMEOUT:
                    active_users[user_id] = info
                else:
                    print(f"[Server] Pruned timed-out user: {info['name']} ({user_id})")
            users.clear()
            users.update(active_users)

# --- Flask App ---

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

@app.route('/')
def index():
    """Serves the main HTML page."""
    # The name is now handled by the client-side login
    return render_template('index.html')

@app.route('/login', methods=['POST'])
def login():
    """Logs in a new user."""
    try:
        data = request.json
        name = data.get('name')
        if not name:
            return jsonify({"status": "error", "message": "Name is required"}), 400
        
        user_id = str(uuid.uuid4())
        with users_lock:
            users[user_id] = {"name": name, "last_seen": time.time()}
        
        print(f"[Server] User logged in: {name} as {user_id}")
        return jsonify({"status": "ok", "user_id": user_id, "name": name})
    except Exception as e:
        print(f"[Login Error] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    """Receives a heartbeat from a user to keep them online."""
    try:
        data = request.json
        user_id = data.get('user_id')
        if user_id and user_id in users:
            with users_lock:
                users[user_id]["last_seen"] = time.time()
            return jsonify({"status": "ok"})
        return jsonify({"status": "error", "message": "User not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/users')
def get_users():
    """Returns the list of currently online users."""
    with users_lock:
        # Return a list of {"id": ..., "name": ...}
        user_list = [{"id": uid, "name": info["name"]} for uid, info in users.items()]
        return jsonify(user_list)

@app.route('/send/message', methods=['POST'])
def send_message():
    """Receives a message and adds it to the global queue."""
    try:
        data = request.json
        from_user_id = data.get('from_user_id')
        to_user_id = data.get('to_user_id') # "all" or a specific user_id
        message = data.get('message')

        if not all([from_user_id, to_user_id, message]):
            return jsonify({"status": "error", "message": "Missing fields"}), 400
        
        from_name = users.get(from_user_id, {}).get("name", "Unknown")

        msg_entry = {
            "msg_id": str(uuid.uuid4()),
            "from_name": from_name,
            "from_user_id": from_user_id,
            "to_user_id": to_user_id,
            "type": "message",
            "content": message,
            "timestamp": time.time()
        }
        
        with messages_lock:
            messages.append(msg_entry)
        
        print(f"Message from {from_name} to {to_user_id}: {message}")
        return jsonify({"status": "ok"})
    
    except Exception as e:
        print(f"[Send Message Error] {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/send/file', methods=['POST'])
def send_file():
    """Receives a file, saves it, and adds a file message to the queue."""
    try:
        if 'file' not in request.files:
            return jsonify({"status": "error", "message": "No file part"}), 400
        
        file = request.files['file']
        from_user_id = request.form.get('from_user_id')
        to_user_id = request.form.get('to_user_id') # "all" or specific user_id

        if not all([file, from_user_id, to_user_id]):
            return jsonify({"status": "error", "message": "Missing fields"}), 400

        if file.filename == '':
            return jsonify({"status": "error", "message": "No selected file"}), 400

        filename = secure_filename(file.filename)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(save_path)
        
        from_name = users.get(from_user_id, {}).get("name", "Unknown")

        file_msg = {
            "msg_id": str(uuid.uuid4()),
            "from_name": from_name,
            "from_user_id": from_user_id,
            "to_user_id": to_user_id,
            "type": "file",
            "content": filename, # Content is the filename
            "timestamp": time.time()
        }
        
        with messages_lock:
            messages.append(file_msg)
        
        print(f"File from {from_name} to {to_user_id}: {filename}")
        return jsonify({"status": "ok", "filename": filename})

    except Exception as e:
        print(f"[Send File Error] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get/messages')
def get_messages():
    """Returns messages for a specific user since a certain time."""
    user_id = request.args.get('user_id')
    since_timestamp = float(request.args.get('since', 0))

    if not user_id:
        return jsonify({"status": "error", "message": "User ID required"}), 400

    relevant_messages = []
    with messages_lock:
        for msg in messages:
            # Check if message is newer and is for me or for "all"
            if msg['timestamp'] > since_timestamp and (msg['to_user_id'] == user_id or msg['to_user_id'] == 'all'):
                relevant_messages.append(msg)
                
    return jsonify(relevant_messages)

@app.route('/download/<filename>')
def download_file(filename):
    """Allows downloading a received file."""
    safe_filename = secure_filename(filename)
    return send_from_directory(app.config['UPLOAD_FOLDER'], safe_filename, as_attachment=True)

# --- Main Execution ---

if __name__ == '__main__':
    # Create uploads folder if it doesn't exist
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    
    # Start pruning thread
    prune_thread = threading.Thread(target=prune_users, daemon=True)
    prune_thread.start()
    
    # Start Flask server
    print(f"\n[Flask Server] Starting Central Hub on http://0.0.0.0:{SERVER_PORT}")
    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False)

