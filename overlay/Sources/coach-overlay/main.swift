// coach-overlay: a glanceable nudge card that floats over Zoom during a live call.
//
// Why it is built this way:
//
// - Native AppKit, not a browser tab. The coach's own web page is useless
//   mid-call: it sits behind the Zoom window, and bringing it forward steals
//   focus from the call. The only thing that can float above a full-screen
//   Zoom meeting without being clicked into is a real window at .floating
//   level that joins every Space.
//
// - A non-activating borderless NSPanel under an .accessory activation policy.
//   The app never gets a dock icon, never takes over the menu bar, and never
//   becomes key or main. Clicking the card to dismiss it is delivered via
//   acceptsFirstMouse without activating the app, so keyboard focus stays in
//   Zoom.
//
// - Dumb display, smart server. All judgement (what to nudge, when, how long)
//   lives in the Python server; this process only renders the latest state of
//   one SSE stream. There is no local state worth persisting, no settings, no
//   permissions (no mic, no screen recording, no accessibility).
//
// - Hidden unless it has something true to say. Idle or disconnected means no
//   window at all; a live call is a 22 pt dot; a nudge is the only time the
//   card is full size, and it fades back to the dot on its own. A stale
//   "listening" dot while the server is down would be a lie, so a dropped
//   stream hides everything until it reconnects.
//
// - SwiftPM + command-line tools, no Xcode project, no Info.plist, no
//   storyboard: one `swift build` produces a single binary.

import AppKit
import Foundation

let defaultBase = "http://127.0.0.1:8140"
let base: String = {
    var raw = ProcessInfo.processInfo.environment["COACH_URL"] ?? defaultBase
    raw = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    while raw.hasSuffix("/") { raw.removeLast() }
    return raw.isEmpty ? defaultBase : raw
}()

// The server refuses a POST unless its Origin is exactly the server's own
// origin (scheme, host AND port), so it is derived from the base URL:
// http://127.0.0.1:8140 by default.
let localOrigin: String = {
    guard let u = URL(string: base), let scheme = u.scheme, let host = u.host else { return defaultBase }
    return u.port.map { "\(scheme)://\(host):\($0)" } ?? "\(scheme)://\(host)"
}()

func postDismiss(_ nudge: Nudge) {
    let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))
    guard
        let call = nudge.callID.addingPercentEncoding(withAllowedCharacters: allowed),
        let id = nudge.id.addingPercentEncoding(withAllowedCharacters: allowed),
        let url = URL(string: "\(base)/coach/live/\(call)/nudges/\(id)/dismiss")
    else { return }
    var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 5)
    req.httpMethod = "POST"
    req.setValue(localOrigin, forHTTPHeaderField: "Origin")
    req.httpBody = Data()
    URLSession.shared.dataTask(with: req) { _, _, _ in }.resume()
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var overlay: OverlayController?
    private var stream: StreamClient?
    private var signalSources: [DispatchSourceSignal] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        let overlay = OverlayController()
        overlay.onDismiss = { postDismiss($0) }

        guard let url = URL(string: "\(base)/coach/live/stream") else {
            FileHandle.standardError.write(Data("coach-overlay: bad COACH_URL \(base)\n".utf8))
            NSApp.terminate(nil)
            return
        }
        let stream = StreamClient(url: url)
        stream.onConnectionChange = { overlay.setConnected($0) }
        stream.onEvent = { overlay.handle($0) }

        self.overlay = overlay
        self.stream = stream

        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in
                self?.stream?.stop()
                NSApp.terminate(nil)
            }
            source.resume()
            signalSources.append(source)
        }

        stream.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        stream?.stop()
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
