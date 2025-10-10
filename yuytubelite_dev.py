from logging import info
import os
import re
import sys
import time
from typing import Dict, List, Optional

from flask import json
import requests
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

APP_NAME = "YUYTube Lite"
APP_VERSION = "v0.6.2"

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
            "eventType": event_type,
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
    finished = QtCore.pyqtSignal(object)
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


class SettingsDialog(QtWidgets.QDialog):
    apiKeySaved = QtCore.pyqtSignal(str)
    messagesSaved = QtCore.pyqtSignal(list)

    def __init__(self, parent: Optional[QtWidgets.QWidget], settings: QtCore.QSettings):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(640, 420)
        self.settings = settings

        apiGroup = QtWidgets.QGroupBox("YouTube Data API Key")
        self.apiEdit = QtWidgets.QLineEdit()
        self.apiEdit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.apiEdit.setPlaceholderText("Enter your API key…")
        self.apiEdit.setText(self.settings.value("api_key", type=str) or os.getenv("YT_API_KEY", ""))

        apiLayout = QtWidgets.QHBoxLayout(apiGroup)
        apiLayout.addWidget(self.apiEdit)

        msgGroup = QtWidgets.QGroupBox("Quick Messages")
        self.listWidget = QtWidgets.QListWidget()
        self.listWidget.addItems([str(s) for s in (self.settings.value("quick_messages", type=list) or [])])

        self.inputEdit = QtWidgets.QLineEdit()
        self.inputEdit.setPlaceholderText("Type a message…")

        self.addBtn = QtWidgets.QPushButton("Add")
        self.addBtn.clicked.connect(self.add_from_input)
        self.pasteAddBtn = QtWidgets.QPushButton("Paste+Add")
        self.pasteAddBtn.clicked.connect(self.paste_and_add)
        self.removeBtn = QtWidgets.QPushButton("Remove Selected")
        self.removeBtn.clicked.connect(self.remove_selected)
        self.clearBtn = QtWidgets.QPushButton("Clear All")
        self.clearBtn.clicked.connect(self.listWidget.clear)

        msgBtnsTop = QtWidgets.QHBoxLayout()
        msgBtnsTop.addWidget(self.inputEdit, 1)
        msgBtnsTop.addWidget(self.addBtn)
        msgBtnsTop.addWidget(self.pasteAddBtn)

        msgBtnsBottom = QtWidgets.QHBoxLayout()
        msgBtnsBottom.addStretch(1)
        msgBtnsBottom.addWidget(self.removeBtn)
        msgBtnsBottom.addWidget(self.clearBtn)

        msgLayout = QtWidgets.QVBoxLayout(msgGroup)
        msgLayout.addWidget(self.listWidget, 1)
        msgLayout.addLayout(msgBtnsTop)
        msgLayout.addLayout(msgBtnsBottom)

        self.saveBtn = QtWidgets.QPushButton("Save")
        self.saveBtn.setDefault(True)
        self.saveBtn.clicked.connect(self.save_and_close)
        self.cancelBtn = QtWidgets.QPushButton("Close")
        self.cancelBtn.clicked.connect(self.reject)

        btnRow = QtWidgets.QHBoxLayout()
        btnRow.addStretch(1)
        btnRow.addWidget(self.saveBtn)
        btnRow.addWidget(self.cancelBtn)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(apiGroup)
        lay.addWidget(msgGroup, 1)
        lay.addLayout(btnRow)

    def add_from_input(self):
        text = self.inputEdit.text().strip()
        if not text:
            return
        self._add_unique(text)
        self.inputEdit.clear()

    def paste_and_add(self):
        text = QGuiApplication.clipboard().text().strip()
        if not text:
            return
        self._add_unique(text)

    def _add_unique(self, text: str):
        existing = [self.listWidget.item(i).text() for i in range(self.listWidget.count())]
        if text not in existing:
            self.listWidget.addItem(text)
            self.listWidget.setCurrentRow(self.listWidget.count() - 1)
        else:
            self.listWidget.setCurrentRow(existing.index(text))

    def remove_selected(self):
        for item in self.listWidget.selectedItems():
            self.listWidget.takeItem(self.listWidget.row(item))

    def save_and_close(self):
        key = self.apiEdit.text().strip()
        self.settings.setValue("api_key", key)
        self.apiKeySaved.emit(key)

        msgs = [self.listWidget.item(i).text().strip()
                for i in range(self.listWidget.count())
                if self.listWidget.item(i).text().strip()]
        self.settings.setValue("quick_messages", msgs)
        self.messagesSaved.emit(msgs)
        self.accept()


class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(900, 720)

        self.settings = QtCore.QSettings("YUYTools", "YUYTubeLite")
        self.pool = QtCore.QThreadPool.globalInstance()

        self.handleLabel = QtWidgets.QLabel('Channel @handle or URL:')
        self.handleEdit = QtWidgets.QLineEdit()
        self.handleEdit.setPlaceholderText("@somechannel or https://www.youtube.com/@128kJ")
        self.handleEdit.setText(self.settings.value("last_channel", type=str) or "")

        self.fetchBtn = QtWidgets.QPushButton("Fetch")
        self.fetchBtn.setFixedHeight(26)
        self.fetchBtn.clicked.connect(self.on_fetch)

        topForm = QtWidgets.QGridLayout()
        topForm.setContentsMargins(6, 6, 6, 2)
        topForm.setHorizontalSpacing(6)
        topForm.setVerticalSpacing(4)
        topForm.addWidget(self.handleLabel, 0, 0)
        topForm.addWidget(self.handleEdit, 0, 1)
        topForm.addWidget(self.fetchBtn, 0, 2)

        self.combo = QtWidgets.QComboBox()
        self.combo.setMinimumContentsLength(40)
        self.combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.combo.currentIndexChanged.connect(self.on_combo_changed)

        self.urlLabel = QtWidgets.QLabel("URL:")
        self.urlValue = QtWidgets.QLineEdit()
        self.urlValue.setReadOnly(True)

        self.openBtn = QtWidgets.QPushButton("Open Chat")
        self.openBtn.setFixedHeight(26)
        self.openBtn.clicked.connect(self.open_in_chat_view)

        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(6, 2, 6, 2)
        row.setSpacing(6)
        row.addWidget(self.combo, 1)

        urlRow = QtWidgets.QHBoxLayout()
        urlRow.setContentsMargins(6, 2, 6, 2)
        urlRow.setSpacing(6)
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

        self.webView = QWebEngineView()
        self.webPage = QWebEnginePage(self.webProfile, self.webView)
        self.webView.setPage(self.webPage)
        self.webView.setUrl(QUrl(self.settings.value("last_url", type=str) or "https://www.youtube.com/@128kJ"))

        # Inject Plugins on page finish
        #self.webView.loadFinished.connect(self._inject_dev_plugin)
        #self.webView.urlChanged.connect(lambda _u: self._inject_dev_plugin(True))
        self.webView.loadFinished.connect(self._inject_chat_shortcuts)

        # Compact quick messages
        self.quickCombo = QtWidgets.QComboBox()
        self.quickCombo.setEditable(False)
        self.quickCombo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.quickCombo.setMinimumContentsLength(24)
        self.quickCombo.setMaximumHeight(24)
        self.quickCombo.setStyleSheet("""
            QComboBox { font-size: 11px; padding: 1px 4px; }
            QComboBox QAbstractItemView { font-size: 11px; }
        """)

        def tiny_tool_button(text: str, tooltip: str) -> QtWidgets.QToolButton:
            btn = QtWidgets.QToolButton()
            btn.setText(text)
            btn.setToolTip(tooltip)
            btn.setAutoRaise(True)
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(22)
            btn.setMinimumWidth(28 if text == "⚙️" else 40)
            btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
            return btn

        self.copyBtn = tiny_tool_button("Copy", "Copy the selected quick message")
        self.copyBtn.clicked.connect(self.copy_selected_message)
        self.loadTemplatesBtn = tiny_tool_button("Load", "Load template messages")
        self.loadTemplatesBtn.clicked.connect(self.load_default_messages)
        self.editBtn = tiny_tool_button("⚙️", "Open settings")
        self.editBtn.clicked.connect(self.open_settings_dialog)

        bottomBar = QtWidgets.QHBoxLayout()
        bottomBar.setContentsMargins(6, 2, 6, 6)
        bottomBar.setSpacing(4)
        bottomBar.addWidget(self.quickCombo, 1)
        bottomBar.addWidget(self.copyBtn)
        bottomBar.addWidget(self.loadTemplatesBtn)
        bottomBar.addWidget(self.editBtn)

        self.status = QtWidgets.QLabel("Ready.")
        self.status.setStyleSheet("color:#666;")
        self.status.setMaximumHeight(18)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        lay.addLayout(topForm)
        lay.addLayout(row)
        lay.addLayout(urlRow)
        lay.addWidget(self.webView, 1)
        lay.addLayout(bottomBar)
        lay.addWidget(self.status)

        self.load_saved_messages()

    # ----------- Safe injector (no innerHTML, no string HTML) -----------
    def _inject_chat_shortcuts(self, ok: bool):
        if not ok:
            return
        url = self.webView.url().toString()
        if "youtube.com" not in url:
            return
        js = r'''(function(){
      try{
        if (window.__yuy_chat_patch_twitchlike_v2__) return;
        window.__yuy_chat_patch_twitchlike_v2__ = true;

        // keep only Remove + Timeout
        function keepOnlyModerationButtons(root){
          if (!root) return;
          root.querySelectorAll('yt-button-renderer').forEach(item=>{
            const btn = item.querySelector('button');
            const label = (btn && (btn.getAttribute('aria-label') || btn.title || '')).toLowerCase();
            const keep = /(^|\s)(remove|put user in timeout)(\s|$)/.test(label);
            item.style.display = keep ? 'inline-flex' : 'none';
          });
        }

        // prevent chat message click handlers from firing when using our buttons, doesn't work sighhh
        function silenceBubbling(el){
            if (!el) return;
            const stop = (e)=>{ e.stopPropagation(); e.cancelBubble = true; };
            // bubble-phase only; DO NOT use capture or stopImmediatePropagation
            ['click','mouseup','pointerup','touchend'].forEach(ev=>{
            el.addEventListener(ev, stop, false);
            });
        }

        function ensureInlineBox(msg){
          let box = msg.querySelector('#inline-action-button-container');
          if (!box){
            box = document.createElement('div');
            box.id = 'inline-action-button-container';
            box.innerHTML = '<div id="inline-action-buttons"></div>';
            msg.appendChild(box);
          }
          box.setAttribute('aria-hidden','false');
          return box;
        }

        function sizeDownButtons(btns){
            if (!btns) return;
                btns.querySelectorAll('button').forEach(b=>{
                b.style.height = '16px';
                b.style.width  = '16px';
                b.style.minHeight = '16px';
                b.style.minWidth  = '16px';
                b.style.padding = '0';
                b.style.lineHeight = '16px';
                const svg = b.querySelector('svg');
                if (svg){ svg.setAttribute('width','14'); svg.setAttribute('height','14'); }
                    // stop only bubbling (allow default + target handlers)
                    silenceBubbling(b);
                });
            silenceBubbling(btns);
        }

        function patchMessage(msg){
          if (!msg || msg.nodeType !== 1) return;
          if (msg.getAttribute('data-yuy-patched') === '1') return;

          const content  = msg.querySelector('#content');
          const menuWrap = msg.querySelector('#menu');
          let   box      = ensureInlineBox(msg);
          const btns     = box.querySelector('#inline-action-buttons');

          msg.setAttribute('data-yuy-patched','1');

          if (btns){
            btns.style.display = 'inline-flex';
            btns.style.gap = '4px';          // tighter spacing
            keepOnlyModerationButtons(btns);
            sizeDownButtons(btns);
          }
          box.style.background = 'transparent';
          box.style.opacity = '1';
          box.style.visibility = 'visible';

          // Move inline actions BEFORE the text (Twitch style)
          if (content && box !== content.previousElementSibling){
            content.parentNode.insertBefore(box, content);
          }

          // Keep ⋮ visible, but don't absolute-position
          if (menuWrap){
            menuWrap.style.display = 'inline-flex';
            menuWrap.style.opacity = '1';
            menuWrap.style.visibility = 'visible';
          }
        }

        function sweep(root){
          (root.querySelectorAll ?
           root.querySelectorAll('yt-live-chat-text-message-renderer') : []
          ).forEach(patchMessage);
        }

        // Grid each message => [avatar][controls][text][⋮]
        if (!document.getElementById('yuy-chat-twitchlike-style-v2')) {
          const s = document.createElement('style');
          s.id = 'yuy-chat-twitchlike-style-v2';
          s.textContent =
            'yt-live-chat-text-message-renderer[data-yuy-patched="1"]{'
              + 'display:grid !important;'
              + 'grid-template-columns:auto auto 1fr auto !important;'
              + 'align-items:center !important;'
              + 'column-gap:6px !important;'
            + '}'
            + 'yt-live-chat-text-message-renderer[data-yuy-patched="1"] #author-photo{grid-column:1 !important;}'
            + 'yt-live-chat-text-message-renderer[data-yuy-patched="1"] #inline-action-button-container{'
              + 'grid-column:2 !important;'
              + 'position:static !important;'
              + 'display:inline-flex !important;'
              + 'align-items:center !important;'
              + 'gap:4px !important;'
              + 'background:transparent !important;'
            + '}'
            + 'yt-live-chat-text-message-renderer[data-yuy-patched="1"] #inline-action-buttons{'
              + 'display:inline-flex !important;'
              + 'gap:4px !important;'
            + '}'
            + 'yt-live-chat-text-message-renderer[data-yuy-patched="1"] #content{'
              + 'grid-column:3 !important;'
              + 'overflow:visible !important;'
              + 'padding-right:0 !important;'
            + '}'
            + 'yt-live-chat-text-message-renderer[data-yuy-patched="1"] #menu{'
              + 'grid-column:4 !important;'
              + 'position:static !important;'
              + 'display:inline-flex !important;'
              + 'opacity:1 !important;'
              + 'visibility:visible !important;'
            + '}'
            // remove ripple/overlay only inside patched messages
            + 'yt-live-chat-text-message-renderer[data-yuy-patched="1"] yt-touch-feedback-shape,'
            + 'yt-live-chat-text-message-renderer[data-yuy-patched="1"] .yt-spec-touch-feedback-shape{display:none !important;}';
          (document.head || document.documentElement).appendChild(s);
        }

        // First pass
        sweep(document);

        // Keep patching as new messages arrive
        const mo = new MutationObserver(muts=>{
          let touched=false;
          for (const m of muts){
            if (m.type==='childList' && m.addedNodes && m.addedNodes.length){
              m.addedNodes.forEach(n=>{
                if (n.nodeType!==1) return;
                if (n.matches && n.matches('yt-live-chat-text-message-renderer')){
                  patchMessage(n); touched=true;
                } else {
                  const msgs = n.querySelectorAll ? n.querySelectorAll('yt-live-chat-text-message-renderer') : [];
                  if (msgs && msgs.length){ msgs.forEach(patchMessage); touched=true; }
                }
              });
            }
          }
          if (!touched) sweep(document);
        });
        mo.observe(document.documentElement, {childList:true, subtree:true});

        console.log('[Chat Patch] controls injected');
      }catch(e){
        console.error('[Chat Patch] Injection error:', e);
      }
    })();'''
        self.webView.page().runJavaScript(js)

    # ---------- Persistence helpers ----------
    def _save_settings(self):
        self.settings.setValue("last_channel", self.handleEdit.text())
        current_url = self.webView.url().toString() if self.webView.url().isValid() else ""
        self.settings.setValue("last_url", current_url)

    def closeEvent(self, event):
        self._save_settings()
        self.save_current_messages()
        super().closeEvent(event)

    # ---------- Actions ----------
    def on_fetch(self):
        api_key = (self.settings.value("api_key", type=str) or "").strip() or None
        channel_input = self.handleEdit.text().strip()

        if not api_key:
            self.set_status("No API key set. Click ⚙️ to add your YouTube Data API key.", error=True)
            return
        if not channel_input:
            self.set_status("Enter an @handle or channel URL/ID.", error=True)
            return

        self.fetchBtn.setEnabled(False)
        self.set_status("Fetching…")
        self.combo.clear()
        self.urlValue.clear()

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

    # ---------- Quick messages ----------
    def load_default_messages(self):
        templates = [
            "🩵 Twitch: https://twitch.tv/yuy_ix 🩵 Discord: https://discord.gg/yuy 🩵 X: https://x.com/YUY_IX 🩵",
            "🩵 TTS IS CURRENTLY DISABLED 🩵",
            "THANK YOU CHAT🩵"
        ]
        self.quickCombo.clear()
        self.quickCombo.addItems(templates)
        self.set_status(f"Loaded {len(templates)} quick messages. Use ⚙️ to save/edit.")

    def load_saved_messages(self):
        saved = self.settings.value("quick_messages", type=list) or []
        if saved:
            self.quickCombo.clear()
            self.quickCombo.addItems([str(s) for s in saved])
            self.set_status(f"Loaded {len(saved)} saved quick messages.")
        else:
            self.set_status("No saved quick messages. Load Templates or use ⚙️ to add your own.")

    def save_current_messages(self):
        msgs = [self.quickCombo.itemText(i).strip()
                for i in range(self.quickCombo.count())
                if self.quickCombo.itemText(i).strip()]
        self.settings.setValue("quick_messages", msgs)

    def copy_selected_message(self):
        msg = self.quickCombo.currentText().strip()
        if not msg:
            self.set_status("No quick message selected.", error=True)
            return
        QGuiApplication.clipboard().setText(msg)
        self.set_status("Copied quick message to clipboard.")

    # ---------- Settings dialog ----------
    def open_settings_dialog(self):
        dlg = SettingsDialog(self, self.settings)
        dlg.apiKeySaved.connect(self._on_api_key_saved)
        dlg.messagesSaved.connect(self._apply_saved_messages_from_dialog)
        dlg.exec()

    def _apply_saved_messages_from_dialog(self, messages: List[str]):
        self.quickCombo.clear()
        self.quickCombo.addItems(messages)
        if messages:
            self.quickCombo.setCurrentIndex(0)
        self.set_status(f"Saved {len(messages)} quick messages.")

    def _on_api_key_saved(self, key: str):
        self.set_status("API key saved." if key else "API key cleared.")


def main():
    QtCore.QCoreApplication.setOrganizationName("YUYTools")
    QtCore.QCoreApplication.setApplicationName("YUYTubeLite")

    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
