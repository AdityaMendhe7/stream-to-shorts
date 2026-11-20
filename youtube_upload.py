"""YouTube uploader for clipper.py (YouTube Data API v3, OAuth desktop flow)."""
import sys
import os
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
HERE = Path(__file__).resolve().parent
SECRETS = Path(os.environ.get("YOUTUBE_CLIENT_SECRET_PATH", HERE / "client_secret.json"))
TOKEN = Path(os.environ.get("YOUTUBE_TOKEN_PATH", HERE / "token.json"))
_service = None


def _get_service():
    global _service
    if _service:
        return _service
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("Google libraries missing. Run: pip install -r requirements.txt")

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None  # token revoked/expired -> sign in again
        if not creds or not creds.valid:
            if not SECRETS.exists():
                sys.exit(f"Missing YouTube OAuth client file at {SECRETS}. Set "
                         "YOUTUBE_CLIENT_SECRET_PATH or place client_secret.json next to the scripts.")
            flow = InstalledAppFlow.from_client_secrets_file(str(SECRETS), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN.write_text(creds.to_json())
    _service = build("youtube", "v3", credentials=creds)
    return _service


def upload_video(path, title, privacy="private", tags=None, description=""):
    """Upload one video. Returns the YouTube video id."""
    from googleapiclient.http import MediaFileUpload

    yt = _get_service()
    body = {
        "snippet": {
            "title": title[:100],
            "description": (description + "\n\n#Shorts").strip(),
            "tags": tags or [],
            "categoryId": "20",  # Gaming
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True,
                            chunksize=8 * 1024 * 1024)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            print(f"    uploading... {int(status.progress() * 100)}%")
    return resp["id"]
