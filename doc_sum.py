import os
import sys
import time
import mimetypes
import requests
import json
from pathlib import Path
from dotenv import load_dotenv

# Load .env for AMPLIFY_API_KEY
load_dotenv()

AMPLIFY_API_KEY = os.getenv("AMPLIFY_API_KEY")
if not AMPLIFY_API_KEY:
    print("❌ Missing AMPLIFY_API_KEY in environment. Add it to your .env file.")
    sys.exit(1)

API_BASE = "https://prod-api.vanderbilt.ai"

# ---------------------------
# Upload file to Amplify
# ---------------------------
def upload_file_to_amplify(file_path, knowledge_base="summaries", tags=None):
    file_name = os.path.basename(file_path)
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "application/octet-stream"

    upload_url = f"{API_BASE}/files/upload"
    headers = {
        "Authorization": f"Bearer {AMPLIFY_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "type": mime_type,
        "name": file_name,
        "knowledgeBase": knowledge_base,
        "tags": tags or ["document_summary"],
        "actions": [
            {"name": "saveAsData"},
            {"name": "createChunks"},
            {"name": "ingestRag"},
            {"name": "makeDownloadable"},
            {"name": "extractText"},
        ],
        "ragOn": True,
    }

    print("🔎 Upload payload:", json.dumps(payload, indent=2))

    try:
        response = requests.post(upload_url, json=payload, headers=headers, timeout=50)
        response.raise_for_status()

        upload_response = response.json()
        print("✅ Upload response:", json.dumps(upload_response, indent=2))

        presigned_url = upload_response.get("uploadUrl")
        if presigned_url:
            print("⏳ Uploading file bytes to presigned URL...")
            with open(file_path, "rb") as f:
                put_resp = requests.put(presigned_url, data=f)
                put_resp.raise_for_status()
            print("✅ File bytes uploaded successfully")

        return upload_response
    except requests.exceptions.RequestException as e:
        print(f"❌ Upload error: {e}")
        if "response" in locals():
            try:
                print("⚠️ Server response:", response.text)
            except Exception:
                pass
        return None


# ---------------------------
# Wait for file processing
# ---------------------------
def wait_for_file_processing(file_id, max_attempts=10, wait_seconds=20):
    status_url = f"{API_BASE}/files/{file_id}/status"
    headers = {"Authorization": f"Bearer {AMPLIFY_API_KEY}"}

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(status_url, headers=headers, timeout=30)
            response.raise_for_status()
            status_data = response.json()
            state = status_data.get("status")
            print(f"⏳ Attempt {attempt}/{max_attempts} - File status: {state}")

            if state == "ready":
                print("✅ File is ready!")
                return True
            elif state == "failed":
                print("❌ File processing failed.")
                return False
        except Exception as e:
            print(f"⚠️ Error checking status: {e}")

        time.sleep(wait_seconds)

    print("❌ File did not become available in time.")
    return False


# ---------------------------
# Summarize the document
# ---------------------------
def summarize_document(file_path):
    print("=== Document Summarization Pipeline (Single File) ===")
    print(f"📄 File: {file_path}")

    upload_response = upload_file_to_amplify(file_path)
    if not upload_response:
        print("❌ Upload failed.")
        return

    file_id = upload_response.get("id")
    if not file_id:
        print("❌ No file ID returned from API. Response was:")
        print(json.dumps(upload_response, indent=2))
        return

    if not wait_for_file_processing(file_id):
        return

    # Ask the LLM to summarize
    summarize_url = f"{API_BASE}/responses"
    headers = {
        "Authorization": f"Bearer {AMPLIFY_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "input": f"Please summarize the document: {os.path.basename(file_path)}",
        "fileIds": [file_id],
        "conversation": "reuse",
    }

    try:
        response = requests.post(summarize_url, json=payload, headers=headers, timeout=120)
        response.raise_for_status()
        result = response.json()
        print("📑 Summary:", result.get("outputText", "No summary returned."))
    except requests.exceptions.RequestException as e:
        print(f"❌ Summarization error: {e}")
        if "response" in locals():
            try:
                print("⚠️ Server response:", response.text)
            except Exception:
                pass


# ---------------------------
# Entry point
# ---------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python doc_sum.py <file_path>")
        sys.exit(1)

    file_path = sys.argv[1]
    if not Path(file_path).is_file():
        print(f"❌ File not found: {file_path}")
        sys.exit(1)

    summarize_document(file_path)


