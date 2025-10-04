import os
import re
import sys
import time
from typing import Dict, List, Optional

import requests
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

CHANNEL_ID_RE = re.compile(r"(?:^|/)(UC[0-9A-Za-z_-]{22})(?:$|/)")
HANDLE_RE = re.compile(r"(?:^|/)@([A-Za-z0-9._-]+)(?:$|/)")
USER_RE = re.compile(r"(?:^|/)user/([A-Za-z0-9._-]+)(?:$|/)")
CUSTOM_C_RE = re.compile(r"(?:^|/)c/([A-Za-z0-9._-]+)(?:$|/)")
VID_RE = re.compile(r"[?&]v=([0-9A-Za-z_-]{11})|/live/([0-9A-Za-z_-]{11})|/shorts/([0-9A-Za-z_-]{11})|/watch/([0-9A-Za-z_-]{11})")


def _req_get(url: str, params: Dict, api_key: Optional[str], debug: bool = False) -> Dict:
    if api_key:
        params = dict(params) | {"key": api_key}
    backoff = 1.0
    for _ in range(5):
        r = requests.get(url, params=params, timeout=20)
        if debug:
            print(f"[GET] {r.url} -> {r.status_code}")
        if r.status_code == 200:
            return r.json()
        if r.status_code in (401, 403, 429, 500, 503):
            time.sleep(backoff)
            backoff *= 2
            continue
        try:
            j = r.json()
            raise RuntimeError(f"HTTP {r.status_code}: {j.get('error', {}).get('message', r.text)}")
        except Exception:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
    raise RuntimeError("Exceeded retry attempts (rate limits or temporary errors).")


def _extract_bits(raw: str) -> Dict[str, Optional[str]]:
    txt = raw.strip()
    m = CHANNEL_ID_RE.search(txt)
    if m:
        return {"channel_id": m.group(1), "handle": None, "username": None, "custom": None}
    if txt.startswith("@"):
        return {"channel_id": None, "handle": txt[1:], "username": None, "custom": None}
    m = HANDLE_RE.search(txt)
    if m:
        return {"channel_id": None, "handle": m.group(1), "username": None, "custom": None}
    m = USER_RE.search(txt)
    if m:
        return {"channel_id": None, "handle": None, "username": m.group(1), "custom": None}
    m = CUSTOM_C_RE.search(txt)
    if m:
        return {"channel_id": None, "handle": None, "username": None, "custom": m.group(1)}
    if txt.startswith("UC") and len(txt) >= 24:
        return {"channel_id": txt, "handle": None, "username": None, "custom": None}
    return {"channel_id": None, "handle": None, "username": None, "custom": txt}


def _channels_for_handle(handle: str, api_key: Optional[str], debug: bool = False) -> Optional[str]:
    params = {"part": "id", "forHandle": handle, "maxResults": 1}
    try:
        data = _req_get(f"{YOUTUBE_API_BASE}/channels", params, api_key, debug)
        items = data.get("items") or []
        if items:
            return items[0]["id"]
    except Exception:
        pass
    return None


def _channels_for_username(username: str, api_key: Optional[str], debug: bool = False) -> Optional[str]:
    params = {"part": "id", "forUsername": username, "maxResults": 1}
    data = _req_get(f"{YOUTUBE_API_BASE}/channels", params, api_key, debug)
    items = data.get("items") or []
    if items:
        return items[0]["id"]
    return None


def _search_channel_id(query: str, api_key: Optional[str], debug: bool = False) -> Optional[str]:
    params = {"part": "snippet", "q": query, "type": "channel", "maxResults": 1}
    data = _req_get(f"{YOUTUBE_API_BASE}/search", params, api_key, debug)
    items = data.get("items") or []
    if not items:
        return None
    return items[0]["snippet"]["channelId"]


def resolve_channel_id(channel_input: str, api_key: Optional[str], debug: bool = False) -> str:
    bits = _extract_bits(channel_input)
    if bits["channel_id"]:
        return bits["channel_id"]
    if bits["handle"]:
        cid = _channels_for_handle(bits["handle"], api_key, debug) or _search_channel_id(f"@{bits['handle']}", api_key, debug)
        if cid:
            return cid
    if bits["username"]:
        cid = _channels_for_username(bits["username"], api_key, debug) or _search_channel_id(bits["username"], api_key, debug)
        if cid:
            return cid
    if bits["custom"]:
        cid = _search_channel_id(bits["custom"], api_key, debug)
        if cid:
            return cid
    cid = _search_channel_id(channel_input, api_key, debug)
    if cid:
        return cid
    raise RuntimeError(f"Could not resolve channel from input: {channel_input}")


def _search_live_videos(channel_id: str, event_type: str, limit: int, api_key: Optional[str], debug: bool = False) -> List[Dict]:
    url = f"{YOUTUBE_API_BASE}/search"
    all_items: List[Dict] = []
    page_token = None
    fetched = 0
    while True:
        count = min(50, limit - fetched)
        if count <= 0:
            break
        params = {
            "part": "snippet",
            "channelId": channel_id,
            "type": "video",
            "eventType": event_type,  # live | upcoming
            "order": "date",
            "maxResults": count,
        }
        if page_token:
            params["pageToken"] = page_token
        data = _req_get(url, params, api_key, debug)
        items = data.get("items") or []
        all_items.extend(items)
        fetched += len(items)
        page_token = data.get("nextPageToken")
        if not page_token or fetched >= limit:
            break
    return all_items


def _search_recent_upload_ids(channel_id: str, limit: int, api_key: Optional[str], debug: bool = False) -> List[str]:
    url = f"{YOUTUBE_API_BASE}/search"
    vids: List[str] = []
    page_token = None
    fetched = 0
    while True:
        count = min(50, limit - fetched)
        if count <= 0:
            break
        params = {
            "part": "snippet",
            "channelId": channel_id,
            "type": "video",
            "order": "date",
            "maxResults": count,
        }
        if page_token:
            params["pageToken"] = page_token
        data = _req_get(url, params, api_key, debug)
        items = data.get("items") or []
        for it in items:
            vid = it.get("id", {}).get("videoId")
            if vid:
                vids.append(vid)
        fetched += len(items)
        page_token = data.get("nextPageToken")
        if not page_token or fetched >= limit:
            break
    return vids


def _videos_details(video_ids: List[str], api_key: Optional[str], debug: bool = False) -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    if not video_ids:
        return out
    url = f"{YOUTUBE_API_BASE}/videos"
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i+50]
        params = {"part": "snippet,liveStreamingDetails", "id": ",".join(chunk)}
        data = _req_get(url, params, api_key, debug)
        for v in data.get("items", []):
            out[v["id"]] = {"snippet": v.get("snippet", {}) or {}, "liveStreamingDetails": v.get("liveStreamingDetails", {}) or {}}
    return out


def fetch_live_and_upcoming(channel_input: str, api_key: Optional[str], debug: bool = False) -> List[Dict]:
    channel_id = resolve_channel_id(channel_input, api_key, debug)

    live_items = _search_live_videos(channel_id, "live", 50, api_key, debug)
    upcoming_items = _search_live_videos(channel_id, "upcoming", 50, api_key, debug)

    ids = [it.get("id", {}).get("videoId") for it in (live_items + upcoming_items) if it.get("id", {}).get("videoId")]
    details = _videos_details([vid for vid in ids if vid], api_key, debug)

    def rows_from(items, label):
        rows = []
        for it in items:
            vid = it.get("id", {}).get("videoId")
            det = details.get(vid, {})
            lsd = det.get("liveStreamingDetails", {}) or {}
            sn = det.get("snippet", it.get("snippet", {})) or {}
            rows.append({
                "status": "LIVE" if label == "live" else "UPCOMING",
                "title": sn.get("title", it.get("snippet", {}).get("title", "")),
                "videoId": vid,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "scheduledStartTime": lsd.get("scheduledStartTime"),
                "actualStartTime": lsd.get("actualStartTime"),
            })
        return rows

    results = rows_from(live_items, "live") + rows_from(upcoming_items, "upcoming")

    if not results:
        # fallback scan recent uploads 
        recent_ids = _search_recent_upload_ids(channel_id, 120, api_key, debug)
        det = _videos_details(recent_ids, api_key, debug)
        for vid, d in det.items():
            lsd = d.get("liveStreamingDetails", {}) or {}
            sn = d.get("snippet", {}) or {}
            if lsd.get("actualStartTime") and not lsd.get("actualEndTime"):
                results.append({"status": "LIVE", "title": sn.get("title",""), "videoId": vid,
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "scheduledStartTime": lsd.get("scheduledStartTime"),
                                "actualStartTime": lsd.get("actualStartTime")})
            elif lsd.get("scheduledStartTime") and not lsd.get("actualStartTime"):
                results.append({"status": "UPCOMING", "title": sn.get("title",""), "videoId": vid,
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "scheduledStartTime": lsd.get("scheduledStartTime"),
                                "actualStartTime": lsd.get("actualStartTime")})

    # LIVE first then upcoming by time
    dedup: Dict[str, Dict] = {}
    for r in results:
        dedup[r["videoId"]] = r
    results = list(dedup.values())

    def sort_key(r):
        if r["status"] == "LIVE":
            return (0, r.get("actualStartTime") or "", r["title"])
        return (1, r.get("scheduledStartTime") or "9999", r["title"])
    results.sort(key=sort_key)
    return results


class WorkerSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(object)  # List
    failed = QtCore.pyqtSignal(str)


class FetchWorker(QtCore.QRunnable):
    def __init__(self, channel_input: str, api_key: Optional[str], debug: bool = False):
        super().__init__()
        self.channel_input = channel_input
        self.api_key = api_key
        self.debug = debug
        self.signals = WorkerSignals()

    @QtCore.pyqtSlot()
    def run(self):
        try:
            rows = fetch_live_and_upcoming(self.channel_input, self.api_key, self.debug)
            self.signals.finished.emit(rows)
        except Exception as e:
            self.signals.failed.emit(str(e))


class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YUYTube Lite v0.4")
        self.resize(900, 700)

        # settings stored in OS settings store.
        self.settings = QtCore.QSettings("YUYTools", "YUYTubeLite")

        self.pool = QtCore.QThreadPool.globalInstance()

        # inputs
        self.apiLabel = QtWidgets.QLabel("YouTube API Key:")
        self.apiEdit = QtWidgets.QLineEdit()
        self.apiEdit.setPlaceholderText("YoutubeDataV3 Key or set the .env YT_API_KEY")
        # Load saved API key or fallback to env var
        saved_key = self.settings.value("api_key", type=str) or os.getenv("YT_API_KEY", "")
        self.apiEdit.setText(saved_key)
        self.apiEdit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)

        self.handleLabel = QtWidgets.QLabel('Channel @handle or URL:')
        self.handleEdit = QtWidgets.QLineEdit()
        self.handleEdit.setPlaceholderText("@somechannel or https://www.youtube.com/@128kJ")
        self.handleEdit.setText(self.settings.value("last_channel", type=str) or "")

        self.fetchBtn = QtWidgets.QPushButton("Fetch Streams")
        self.fetchBtn.clicked.connect(self.on_fetch)

        topForm = QtWidgets.QGridLayout()
        topForm.addWidget(self.apiLabel, 0, 0)
        topForm.addWidget(self.apiEdit, 0, 1, 1, 2)
        topForm.addWidget(self.handleLabel, 1, 0)
        topForm.addWidget(self.handleEdit, 1, 1)
        topForm.addWidget(self.fetchBtn, 1, 2)

        # results
        self.combo = QtWidgets.QComboBox()
        self.combo.setMinimumContentsLength(50)
        self.combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.combo.currentIndexChanged.connect(self.on_combo_changed)

        self.urlLabel = QtWidgets.QLabel("URL:")
        self.urlValue = QtWidgets.QLineEdit()
        self.urlValue.setReadOnly(True)

        self.openBtn = QtWidgets.QPushButton("Open Chat")
        self.openBtn.clicked.connect(self.open_in_chat_view)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.combo, 1)

        urlRow = QtWidgets.QHBoxLayout()
        urlRow.addWidget(self.urlLabel)
        urlRow.addWidget(self.urlValue, 1)
        urlRow.addWidget(self.openBtn)

        base_dir = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.AppDataLocation
        )
        cache_dir = os.path.join(base_dir, "web_cache")
        storage_dir = os.path.join(base_dir, "web_storage")
        os.makedirs(cache_dir, exist_ok=True)
        os.makedirs(storage_dir, exist_ok=True)

        self.webProfile = QWebEngineProfile("YUYTubeProfile", self)
        self.webProfile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        self.webProfile.setCachePath(cache_dir)
        self.webProfile.setPersistentStoragePath(storage_dir)
        self.webProfile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
        )
        # cache size
        # self.webProfile.setHttpCacheMaximumSize(200 * 1024 * 1024)

        self.webView = QWebEngineView()
        self.webPage = QWebEnginePage(self.webProfile, self.webView)
        self.webView.setPage(self.webPage)
        self.webView.setUrl(QUrl(self.settings.value("last_url", type=str) or "https://www.youtube.com/@128kJ"))

        self.status = QtWidgets.QLabel("Ready.")
        self.status.setStyleSheet("color:#666;")

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(topForm)
        lay.addSpacing(6)
        lay.addLayout(row)
        lay.addLayout(urlRow)
        lay.addWidget(self.webView, 1)
        lay.addWidget(self.status)

    def _save_settings(self):
        self.settings.setValue("api_key", self.apiEdit.text())
        self.settings.setValue("last_channel", self.handleEdit.text())
        # whatever page is currently displayed
        current_url = self.webView.url().toString() if self.webView.url().isValid() else ""
        self.settings.setValue("last_url", current_url)

    def closeEvent(self, event):
        # Save on close
        self._save_settings()
        super().closeEvent(event)

    def on_fetch(self):
        api_key = self.apiEdit.text().strip() or None
        channel_input = self.handleEdit.text().strip()
        if not api_key:
            self.set_status("Supply a YouTube Data API key.", error=True)
            return
        if not channel_input:
            self.set_status("Enter an @handle or channel URL/ID.", error=True)
            return

        self.fetchBtn.setEnabled(False)
        self.set_status("Fetching…")
        self.combo.clear()
        self.urlValue.clear()

        # Save immediately, in case of crash or forced quit
        self._save_settings()

        worker = FetchWorker(channel_input, api_key, debug=False)
        worker.signals.finished.connect(self.on_fetch_finished)
        worker.signals.failed.connect(self.on_fetch_failed)
        self.pool.start(worker)

    def on_fetch_finished(self, rows: List[Dict]):
        self.fetchBtn.setEnabled(True)
        if not rows:
            self.set_status("No LIVE or UPCOMING streams found.", error=False)
            return

        self.combo.clear()
        for r in rows:
            prefix = "🔴" if r["status"] == "LIVE" else "🗓️"
            when = r.get("actualStartTime") if r["status"] == "LIVE" else r.get("scheduledStartTime")
            label = f"{prefix} {r['status']}: {r['title']}"
            if when:
                label += f"  ({when})"
            # store (url, videoId) in userData
            self.combo.addItem(label, (r["url"], r["videoId"]))
        self.combo.setCurrentIndex(0)
        self.on_combo_changed(0)
        self.set_status(f"Loaded {len(rows)} stream(s).")

    def on_fetch_failed(self, message: str):
        self.fetchBtn.setEnabled(True)
        self.set_status(f"Error: {message}", error=True)

    def on_combo_changed(self, idx: int):
        if idx < 0:
            self.urlValue.clear()
            return
        data = self.combo.currentData()
        if not data:
            self.urlValue.clear()
            return
        url, _vid = data
        self.urlValue.setText(url or "")

    def open_in_chat_view(self):
        data = self.combo.currentData()
        if not data:
            self.set_status("Pick a stream first.", error=True)
            return
        _, vid = data
        vid_id = self._extract_video_id(self.urlValue.text()) or vid
        if not vid_id:
            self.set_status("Could not determine video ID.", error=True)
            return
        chat_url = f"https://www.youtube.com/live_chat?is_popout=1&v={vid_id}"
        self.webView.setUrl(QUrl(chat_url))
        self.set_status("Loading chat…")
        # Save the current URL so it restores next launch
        self.settings.setValue("last_url", chat_url)

    def _extract_video_id(self, text: str) -> Optional[str]:
        if not text:
            return None
        m = VID_RE.search(text)
        if not m:
            if len(text.strip()) == 11 and re.fullmatch(r"[0-9A-Za-z_-]{11}", text.strip()):
                return text.strip()
            return None
        for g in m.groups():
            if g:
                return g
        return None

    def set_status(self, text: str, error: bool = False):
        self.status.setText(text)
        self.status.setStyleSheet("color:#C0392B;" if error else "color:#666;")


def main():
    QtCore.QCoreApplication.setOrganizationName("YUYTools")
    QtCore.QCoreApplication.setApplicationName("YUYTubeLite")

    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
