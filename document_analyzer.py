#!/usr/bin/env python3
"""
document_analyzer.py

Scans a directory, uploads files to Amplify, asks Amplify (LLM) to produce a Windows
command prompt script (mkdir/copy/move) that organizes those actual files, and saves
the resulting commands to a .bat file for you to review/run.

Preserves your OneDrive hydration helpers and uses the Amplify endpoints you've been using.
"""

import requests
import json
import os
import time
import datetime
import mimetypes
import sys
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

import platform
import tempfile
import subprocess
import shutil
import ctypes

# Load environment variables from .env file
load_dotenv()


def validate_api_key():
    """Validate that the API key is available"""
    API_KEY = os.getenv("AMPLIFY_API_KEY")
    if not API_KEY:
        print("❌ Error: AMPLIFY_API_KEY not found in environment variables")
        print("Please set your API key in a .env file or environment variable")
        return None
    return API_KEY


def get_headers():
    """Get headers for API requests"""
    API_KEY = validate_api_key()
    if not API_KEY:
        return None
    return {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}


def query_files():
    """Query files already uploaded to Amplify"""
    headers = get_headers()
    if not headers:
        return None

    base_url = "https://prod-api.vanderbilt.ai"
    query_url = f"{base_url}/files/query"

    payload = {
        "data": {
            "startDate": f"{(datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')}",
            "pageSize": 100,
            "pageIndex": 0,
            "forwardScan": True,
            "sortIndex": "createdAt",
            "types": [
                "text/plain",
                "text/x-python",
                "application/json",
                "text/xml",
                "text/html",
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ],
            "tags": [],
        }
    }

    try:
        response = requests.post(query_url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ Error querying files: {e}")
        return None


# Windows attribute constants
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x4000


def get_file_attributes_windows(path: str) -> Optional[int]:
    """Return Windows file attributes or None on error."""
    if platform.system() != "Windows":
        return None
    try:
        GetFileAttributesW = ctypes.windll.kernel32.GetFileAttributesW
        GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        GetFileAttributesW.restype = ctypes.c_uint32
        attrs = GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF:
            return None
        return int(attrs)
    except Exception:
        return None


def is_onedrive_placeholder(path: str) -> bool:
    """Detect if a file is a OneDrive placeholder (likely not hydrated)."""
    attrs = get_file_attributes_windows(path)
    if attrs is None:
        return False
    return bool(attrs & FILE_ATTRIBUTE_OFFLINE or attrs & FILE_ATTRIBUTE_RECALL_ON_OPEN)


def hydrate_file_with_robocopy(path: str, timeout_seconds: int = 1000) -> Optional[str]:
    """
    Use robocopy to copy the single file to a temporary folder, which forces OneDrive to download it.
    Returns path to the local hydrated copy, or None on failure.
    """
    if platform.system() != "Windows":
        return None

    src_dir = os.path.dirname(path)
    file_name = os.path.basename(path)
    tmp_dir = tempfile.mkdtemp(prefix="onedrive_hydrate_")
    # robocopy <source_dir> <dest_dir> <file>  -> copies that single file
    cmd = ["robocopy", src_dir, tmp_dir, file_name, "/J"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_seconds, shell=False)
        dest_path = os.path.join(tmp_dir, file_name)
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path
        else:
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass
            return None
    except Exception:
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass
        return None


def upload_file_to_amplify(
    file_path,
    knowledge_base="documentation",
    tags=None,
    actions=None,
    rag_on=False,
    group_id=None,
):
    """Upload a file to Amplify using the files/upload endpoint"""
    headers = get_headers()
    if not headers:
        return None

    if not os.path.exists(file_path):
        print(f"❌ Error: File not found: {file_path}")
        return None

    temp_copy_dir = None
    file_to_open = file_path
    try:
        # If Windows + OneDrive placeholder, attempt to hydrate via robocopy
        if platform.system() == "Windows" and is_onedrive_placeholder(file_path):
            print(f"🔁 Detected OneDrive placeholder for: {file_path} — attempting to hydrate (robocopy)...")
            hydrated_path = hydrate_file_with_robocopy(file_path)
            if hydrated_path:
                print(f"✅ Hydrated copy created at: {hydrated_path}")
                temp_copy_dir = os.path.dirname(hydrated_path)
                file_to_open = hydrated_path
            else:
                print("⚠️ Hydration via robocopy failed; will try opening the original file directly.")
                file_to_open = file_path
        else:
            file_to_open = file_path

        base_url = "https://prod-api.vanderbilt.ai"
        upload_url = f"{base_url}/files/upload"

        file_name = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = "text/plain"

        if tags is None:
            tags = []
        if actions is None:
            actions = [
                {"name": "saveAsData"},
                {"name": "createChunks"},
                {"name": "ingestRag"},
                {"name": "makeDownloadable"},
                {"name": "extractText"},
            ]

        payload = {
            "data": {
                "type": mime_type,
                "name": file_name,
                "knowledgeBase": knowledge_base,
                "tags": tags,
                "data": {},
                "actions": actions,
                "ragOn": rag_on,
            }
        }
        if group_id is not None:
            payload["data"]["groupId"] = group_id

        # Request upload metadata (presigned URL)
        response = requests.post(upload_url, json=payload, headers=headers, timeout=50
                                )
        response.raise_for_status()
        upload_response = response.json()

        if not upload_response.get("success"):
            print(f"❌ Upload failed - {upload_response.get('error', 'Unknown error')}")
            return None

        presigned_url = upload_response.get("uploadUrl")
        if not presigned_url:
            print("❌ Error: No upload URL received from server")
            return None

        # Read the (possibly hydrated) file contents
        with open(file_to_open, "rb") as file:
            file_content = file.read()

        s3_headers = {"Content-Type": mime_type}
        s3_response = requests.put(presigned_url, data=file_content, headers=s3_headers, timeout=1000)
        s3_response.raise_for_status()

        print(f"✅ File uploaded successfully: {file_name}")
        return upload_response

    except requests.exceptions.RequestException as e:
        print(f"❌ Error uploading file: {e}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error during upload: {e}")
        return None
    finally:
        # Cleanup any temporary hydrated copy directory
        if temp_copy_dir and os.path.isdir(temp_copy_dir):
            try:
                shutil.rmtree(temp_copy_dir)
                print(f"🧹 Cleaned up temporary hydrated files: {temp_copy_dir}")
            except Exception:
                pass


def chat_with_amplify(
    model,
    temperature,
    max_tokens,
    data_source_ids,
    message,
    system_message=None,
    assistant_id=None,
):
    """Chat with Amplify using uploaded files or assistants"""
    headers = get_headers()
    if not headers:
        return None

    base_url = "https://prod-api.vanderbilt.ai"
    chat_url = f"{base_url}/chat"

    messages = []
    if system_message:
        messages.append({"role": "system", "content": system_message})
    messages.append({"role": "user", "content": message})

    payload = {
        "data": {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
            "dataSources": data_source_ids,
            "options": {
                "ragOnly": False,
                "skipRag": False,
                "model": {"id": model},
                "prompt": message,
            },
        }
    }

    if assistant_id is not None:
        payload["data"]["options"]["assistantId"] = assistant_id

    try:
        response = requests.post(chat_url, json=payload, headers=headers, timeout=1000)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ Error chatting with Amplify: {e}")
        return None


def wait_for_file_processing(file_name, max_attempts=2, wait_seconds=20):
    """Wait for a file to be processed and available for use"""
    print(f"⏳ Waiting for file '{file_name}' to be processed...")

    for attempt in range(1, max_attempts + 1):
        print(f"  Attempt {attempt}/{max_attempts} - Checking if file is available...")
        files_response = query_files()
        if not files_response:
            print("  ❌ Could not query files")
            time.sleep(wait_seconds)
            continue
        files_list = files_response.get("data", {}).get("items", [])
        for file_info in files_list:
            if file_info.get("name") == file_name:
                file_id = file_info.get("id")
                print(f"✅ File is now available! ID: {file_id}")
                return file_id
        if attempt < max_attempts:
            print(f"  File not ready yet. Waiting {wait_seconds} seconds...")
            time.sleep(wait_seconds)
    print("❌ File did not become available in time")
    return None


def get_supported_file_extensions():
    """Get list of supported file extensions"""
    return [
        ".py", ".js", ".ts", ".java", ".cpp", ".c", ".cs", ".php", ".rb", ".go",
        ".rs", ".swift", ".kt", ".scala", ".r", ".m", ".pl", ".sh", ".sql", ".html",
        ".css", ".xml", ".json", ".yaml", ".yml", ".md", ".txt", ".pdf", ".docx", ".pptx", ".xlsx"
    ]


def scan_directory_for_files(directory_path: str) -> List[str]:
    """Scan a directory for supported files"""
    if not os.path.exists(directory_path) or not os.path.isdir(directory_path):
        print(f"❌ Error: Directory not found or not a directory: {directory_path}")
        return []

    supported_extensions = get_supported_file_extensions()
    supported_files = []
    print(f"📂 Scanning directory: {directory_path}")
    for root, dirs, files in os.walk(directory_path):
        dirs[:] = [d for d in dirs if d not in [".git", "__pycache__", "node_modules", ".venv", "venv", "env"]]
        for file in files:
            file_path = os.path.join(root, file)
            file_ext = os.path.splitext(file)[1].lower()
            if file_ext in supported_extensions:
                supported_files.append(file_path)
                print(f"  ✅ Found: {file_path}")
    print(f"📊 Total supported files found: {len(supported_files)}")
    return supported_files


def _extract_text_from_amplify_response(resp: Dict[str, Any]) -> str:
    """
    Robust extraction of textual content from Amplify's chat response.
    Tries several common shapes and falls back to a stringified 'data' if needed.
    """
    if not resp:
        return ""
    # common shape: { "data": { "choices": [ { "message": { "content": "..." } } ] } }
    try:
        choices = resp.get("data", {}).get("choices")
        if isinstance(choices, list) and len(choices) > 0:
            first = choices[0]
            # try nested content keys
            msg = first.get("message") or first.get("response") or {}
            if isinstance(msg, dict):
                content = msg.get("content") or msg.get("text") or msg.get("body")
                if content:
                    return content if isinstance(content, str) else json.dumps(content)
            # direct text field
            text = first.get("text") or first.get("content")
            if text:
                return text if isinstance(text, str) else json.dumps(text)
    except Exception:
        pass

    # fallback: sometimes top-level data contains a string or 'output'
    try:
        data_field = resp.get("data")
        if isinstance(data_field, str):
            return data_field
        if isinstance(data_field, dict):
            # common fallback keys
            for k in ("output", "text", "content"):
                if k in data_field and isinstance(data_field[k], str):
                    return data_field[k]
    except Exception:
        pass

    # As last resort, stringify entire response
    try:
        return json.dumps(resp)
    except Exception:
        return str(resp)

def generate_organization_plan(
    directory_path: str,
    output_dir: str = "organization_plan_output",
    max_files: Optional[int] = None,
    model: str = "gpt-4o-mini"
) -> Dict[str, Any]:
    """Analyzes a directory and generates a Windows CMD organization plan for the actual files present."""
    print(f"=== Document Analysis Pipeline for Organization Plan ===")
    print(f"📂 Source directory: {directory_path}")
    print(f"📁 Output directory: {output_dir}")

    # Step 1: Scan for supported files
    supported_files = scan_directory_for_files(directory_path)
    if not supported_files:
        return {"success": False, "error": "No supported files found"}

    if max_files and len(supported_files) > max_files:
        supported_files = supported_files[:max_files]
        print(f"⚠️ Limited to {max_files} files")

    # Step 2: Upload each file
    file_ids = []
    for file_path in supported_files:
        upload_result = upload_file_to_amplify(
            file_path=file_path,
            knowledge_base="document_analysis",
            tags=["document_analysis", "organization_plan"],
            rag_on=True,
        )
        if upload_result:
            file_id = wait_for_file_processing(os.path.basename(file_path))
            if file_id:
                file_ids.append(file_id)

    if not file_ids:
        return {"success": False, "error": "No files uploaded"}

    # Build list of relative file paths (important so the model doesn't invent files)
    relative_files = [os.path.relpath(path, start=directory_path) for path in supported_files]

    print("\n⏳ Allowing a short pause for RAG/indexing (30s)...")
    time.sleep(30)

    # Step 3: Build prompt — force model to only use the actual files
    organization_prompt = f"""You are a professional document organizer.

Here are the {len(relative_files)} files currently in the directory:
{chr(10).join(relative_files)}

Your task is to generate a valid Windows Command Prompt (.bat) script that organizes
*only these files* into a logical folder structure.

⚠️ Rules:
- Use `mkdir` to create any needed folders.
- Use `move` with the exact relative file paths listed above as the source.
- Use backslashes (`\\`) in all paths.
- Do not invent filenames that are not listed above.
- Output only raw commands line by line, no explanations.
"""

    print("🔎 Sending prompt to Amplify (strict Windows CMD prompt instructions)...")
    organization_result = chat_with_amplify(
        model=model,
        temperature=0.3,
        max_tokens=4000,
        data_source_ids=file_ids,
        message=organization_prompt,
        system_message="You are a professional command-line script generator. Only output mkdir and move commands in valid Windows CMD syntax.",
    )

    if not organization_result:
        return {"success": False, "error": "LLM failed"}

    # Step 4: Save results
    os.makedirs(output_dir, exist_ok=True)
    output_file_path = os.path.join(output_dir, "organization_commands.bat")
    plan_content = organization_result.get("data", "")

    with open(output_file_path, "w", encoding="utf-8") as f:
        f.write(plan_content)

    print(f"✅ Organization commands saved to: {output_file_path}")
    return {"success": True, "output_file_path": output_file_path}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze documents to generate an organization plan.")
    parser.add_argument("directory", type=str, nargs="?", default=".", help="Directory containing documents (default: current dir)")
    parser.add_argument("--output", type=str, default="organization_plan_output", help="Output directory")

    args = parser.parse_args()
    target_directory = args.directory
    output_directory = args.output

    try:
        result = generate_organization_plan(directory_path=target_directory, output_dir=output_directory)
        if not result["success"]:
            print("❌ Pipeline failed:", result.get("error"))
            sys.exit(2)
        else:
            print("🎉 Pipeline execution complete!")
            print("Generated plan at:", result["output_file_path"])
    except KeyboardInterrupt:
        print("\n⚠️ Operation cancelled by user.")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)
