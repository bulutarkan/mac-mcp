"""The Music app (#131): what is playing, the owner's playlists and library, and play, pause and skip.

Reads never launch Music. play opens it in the background when it is closed; every change reads the
player back and answers with the action that reverses it.
"""
from __future__ import annotations

import subprocess
import time
from typing import Any, Dict, Optional

from .app_adapter_core import AppAdapterError, _run_js

_BUNDLE_ID = "com.apple.Music"

_COMMON_JS = """
function fail(code, message) { return JSON.stringify({error: code, message: message, retryable: false}); }
function app() { try { return Application('com.apple.Music'); } catch (e) { return null; } }
function trackOf(t) {
  return {track_id: 'track:' + t.persistentID(), name: t.name(), artist: t.artist() || null,
          album: t.album() || null, duration_s: Math.round(t.duration() || 0)};
}
function state(m) {
  if (!m.running()) return {running: false, state: 'not running', track: null};
  var out = {running: true, state: String(m.playerState()), track: null, position_s: null, playlist: null};
  try { out.track = trackOf(m.currentTrack); out.position_s = Math.round(m.playerPosition() || 0); } catch (e) {}
  try { out.playlist = m.currentPlaylist.name(); } catch (e) {}
  try { out.volume = m.soundVolume(); out.shuffle = m.shuffleEnabled(); out.repeat = String(m.songRepeat()); } catch (e) {}
  return out;
}
"""

_NOW_JS = _COMMON_JS + """
function run(argv) {
  var m = app();
  if (!m) return fail('APP_NOT_INSTALLED', 'Music is not installed on this Mac.');
  return JSON.stringify(state(m));
}
"""

# argv: query, limit.
_PLAYLISTS_JS = _COMMON_JS + """
function run(argv) {
  var m = app();
  if (!m) return fail('APP_NOT_INSTALLED', 'Music is not installed on this Mac.');
  if (!m.running()) return fail('APP_NOT_RUNNING', 'Music is not running; reading never launches it (play does).');
  var needle = argv[0].toLowerCase(), max = Number(argv[1]), lists = m.userPlaylists;
  var names = lists.name(), ids = lists.persistentID(), out = [], matched = 0;
  for (var i = 0; i < names.length; i++) {
    if (needle && names[i].toLowerCase().indexOf(needle) < 0) continue;
    matched += 1;
    if (out.length >= max) continue;
    var p = lists[i], count = null, smart = null;
    try { count = p.tracks.length; } catch (e) {}
    try { smart = p.smart(); } catch (e) {}
    out.push({playlist_id: 'playlist:' + ids[i], name: names[i], track_count: count, smart: smart});
  }
  return JSON.stringify({playlists: out, matched: matched});
}
"""

# argv: query, limit.
_TRACKS_JS = _COMMON_JS + """
function run(argv) {
  var m = app();
  if (!m) return fail('APP_NOT_INSTALLED', 'Music is not installed on this Mac.');
  if (!m.running()) return fail('APP_NOT_RUNNING', 'Music is not running; reading never launches it (play does).');
  var found = m.search(m.libraryPlaylists[0], {for: argv[0]}) || [], max = Number(argv[1]), out = [];
  for (var i = 0; i < found.length && out.length < max; i++) out.push(trackOf(found[i]));
  return JSON.stringify({tracks: out, matched: found.length});
}
"""

# argv: action (play|pause|next_track|previous_track), item id ('' | playlist:<id> | track:<id>).
_CONTROL_JS = _COMMON_JS + """
function run(argv) {
  var m = app();
  if (!m) return fail('APP_NOT_INSTALLED', 'Music is not installed on this Mac.');
  if (!m.running()) return fail('APP_NOT_RUNNING', 'Music is not running.');
  var action = argv[0], item = argv[1], before = state(m);
  if (action === 'play') {
    if (item.indexOf('playlist:') === 0) {
      var lists = m.userPlaylists.whose({persistentID: item.slice(9)});
      if (!lists.length) return fail('MUSIC_ITEM_NOT_FOUND', 'No playlist has that id; find_playlists lists them.');
      m.play(lists[0]);
    } else if (item.indexOf('track:') === 0) {
      var tracks = m.libraryPlaylists[0].tracks.whose({persistentID: item.slice(6)});
      if (!tracks.length) return fail('MUSIC_ITEM_NOT_FOUND', 'No track has that id; find_tracks finds them.');
      m.play(tracks[0]);
    } else if (before.state === 'stopped' && !before.track) {
      // Nothing to resume (Music just opened): play the library, as Music's own play button does.
      m.play(m.libraryPlaylists[0]);
    } else {
      m.play();
    }
  } else if (action === 'pause') {
    m.pause();
  } else {
    if (before.state === 'stopped' || !before.track) {
      return fail('MUSIC_NOTHING_PLAYING', 'Nothing is playing or paused, so there is no track to skip from.');
    }
    if (action === 'next_track') m.nextTrack(); else m.previousTrack();
  }
  delay(0.6);
  return JSON.stringify({before: before, after: state(m)});
}
"""

_REVERSE = {"play": "pause", "pause": "play", "next_track": "previous_track", "previous_track": "next_track"}


def now_playing(*, timeout_s: float) -> Dict[str, Any]:
    return {"ok": True, **_run_js(_NOW_JS, [], timeout_s=timeout_s, code="MUSIC_READ_FAILED")}


def find_playlists(query: Optional[str], *, limit: int, timeout_s: float) -> Dict[str, Any]:
    payload = _run_js(_PLAYLISTS_JS, [str(query or "").strip(), str(limit)], timeout_s=max(timeout_s, 15.0),
                      code="MUSIC_READ_FAILED")
    playlists = payload.get("playlists") or []
    return {"ok": True, "count": len(playlists), "matched": payload.get("matched"), "playlists": playlists,
            "truncated": int(payload.get("matched") or 0) > len(playlists),
            "note": "Play one with play (item_id = its playlist_id)."}


def find_tracks(query: Optional[str], *, limit: int, timeout_s: float) -> Dict[str, Any]:
    text = str(query or "").strip()
    if not text:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "find_tracks needs query = words in the song, artist "
                              "or album.", retryable=False)
    payload = _run_js(_TRACKS_JS, [text, str(limit)], timeout_s=max(timeout_s, 15.0), code="MUSIC_READ_FAILED")
    tracks = payload.get("tracks") or []
    return {"ok": True, "count": len(tracks), "matched": payload.get("matched"), "tracks": tracks,
            "truncated": int(payload.get("matched") or 0) > len(tracks),
            "note": "Play one with play (item_id = its track_id)."}


def _launch(timeout_s: float) -> None:
    """Open Music without bringing it forward, and wait until it answers."""
    try:
        subprocess.run(["open", "-g", "-b", _BUNDLE_ID], capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppAdapterError("MUSIC_OPEN_FAILED", "Music could not be opened.") from exc
    deadline = time.monotonic() + max(3.0, min(timeout_s, 15.0))
    while time.monotonic() < deadline:
        if _run_js(_NOW_JS, [], timeout_s=3.0, code="MUSIC_READ_FAILED").get("running"):
            return
        time.sleep(0.3)
    raise AppAdapterError("MUSIC_OPEN_FAILED", "Music did not start in time.")


def control(action: str, item_id: Optional[str], *, timeout_s: float) -> Dict[str, Any]:
    """play (optionally a playlist_id or track_id), pause, next_track or previous_track, read back after."""
    item = str(item_id or "").strip()
    if item and not item.startswith(("playlist:", "track:")):
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "item_id is a playlist_id from find_playlists or a "
                              "track_id from find_tracks.", retryable=False)
    if action != "play" and item:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"{action} takes no item_id.", retryable=False)
    current = _run_js(_NOW_JS, [], timeout_s=min(timeout_s, 5.0), code="MUSIC_READ_FAILED")
    if not current.get("running"):
        if action == "pause":
            return {"ok": True, "changed": False, "verified": True, "state": "not running",
                    "note": "Music is not running, so nothing plays."}
        if action != "play":
            raise AppAdapterError("APP_NOT_RUNNING", "Music is not running, so there is no track to skip from.",
                                  retryable=False)
        _launch(timeout_s)
    payload = _run_js(_CONTROL_JS, [action, item], timeout_s=max(timeout_s, 10.0), code="MUSIC_CONTROL_FAILED")
    before, after = payload.get("before") or {}, payload.get("after") or {}
    track_before = (before.get("track") or {}).get("track_id")
    track_after = (after.get("track") or {}).get("track_id")
    # Going back more than a few seconds into a song starts that song again.
    restarted = action == "previous_track" and (after.get("position_s") or 0) < (before.get("position_s") or 0)
    if action == "play":
        verified = after.get("state") == "playing" and (not item.startswith("track:") or track_after == item)
    elif action == "pause":
        verified = after.get("state") != "playing"
    else:
        verified = track_after != track_before or restarted
    changed = (before.get("state"), track_before) != (after.get("state"), track_after) or restarted
    result = {"ok": True, "changed": changed, "verified": verified, "before": before, "after": after,
              "reverse_action": _REVERSE[action]}
    if not verified:
        result["note"] = ("Music did not reach the expected state; look at its window with mac_observe "
                          "(an empty library or a track that is not downloaded cannot play).")
    return result
